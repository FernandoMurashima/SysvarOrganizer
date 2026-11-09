"""Página Organização: classificação, prévia (simulação) e revisão de operações.

A prévia é SIMULAÇÃO: nenhum arquivo ou pasta é criado, copiado, movido,
renomeado ou excluído, nem mesmo no destino informado. A seção "Revisão de
operações" registra planos e seleções e, somente em planos de áreas de
homologação autorizadas, valida, executa e reverte (copiar/mover).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidgetItem,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.file_analysis.locations import normalize_location
from sysvar_organizer.engines.file_analysis.models import STATUS_CANCELLED, STATUS_COMPLETED
from sysvar_organizer.engines.organization.categories import CATEGORY_LABELS, category_label
from sysvar_organizer.engines.organization.models import (
    ISSUE_LABELS,
    ITEM_LABELS,
    OrganizationOutcome,
    OrganizationProgress,
    OrganizationRequest,
)
from sysvar_organizer.engines.organization.rules import DATE_SOURCE, MODELS, destination_alerts
from sysvar_organizer.engines.organization.sensitive import KIND_LABELS
from sysvar_organizer.persistence.organization_repository import ItemRecord, SimulationRecord
from sysvar_organizer.ui.formatting import (
    format_bytes,
    format_count,
    format_duration,
    format_epoch,
    format_timestamp,
    status_label,
)
from sysvar_organizer.ui.organization_worker import OrganizationWorker
from sysvar_organizer.ui.pages.compare import SetSelector
from sysvar_organizer.ui.pages.operations_review import OperationsReviewPanel
from sysvar_organizer.ui.widgets import (
    ElidedLabel,
    MetricCard,
    Pager,
    badge,
    card,
    label,
    page_header,
    page_layout,
    result_table,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
SIMULATION_NOTICE = (
    "SIMULAÇÃO — esta tela apenas planeja. Nenhum arquivo ou pasta é criado, copiado, "
    "movido, renomeado ou excluído, nem mesmo no destino informado."
)


def _item(text: str, *, right: bool = False, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    return item


class OrganizationPage(QWidget):
    simulation_finished = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._thread: QThread | None = None
        self._worker: OrganizationWorker | None = None
        self._cancelling = False
        self._busy_check: Callable[[], bool] | None = None
        self._simulation: SimulationRecord | None = None
        self._rows: list[ItemRecord] = []
        self._destination_ok = False
        self._elapsed = QElapsedTimer()
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick)

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Organização",
                "Classifica os arquivos de uma análise concluída e mostra como ficariam "
                "distribuídos em pastas, segundo o modelo escolhido.",
                badge("Somente simulação", "unavailable"),
            )
        )
        notice, notice_box = card("ReservedArea")
        notice_box.addWidget(label(SIMULATION_NOTICE, "SectionTitle", wrap=True))
        layout.addWidget(notice)
        layout.addWidget(self._build_configuration())
        layout.addWidget(self._build_execution())
        layout.addWidget(self._build_summary())
        layout.addWidget(self._build_preview())
        layout.addWidget(self._build_tree())
        self._review = OperationsReviewPanel(context)
        self._review.running_changed.connect(self._update_controls)
        layout.addWidget(self._review)
        layout.addStretch(1)

        self.reload_analyses()
        self._show_latest_completed()
        self._update_controls()

    # Construção ----------------------------------------------------------

    def _build_configuration(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Configuração", "SectionTitle"))
        self._selector = SetSelector("Origem", self._context)
        self._selector.changed.connect(self._validate_destination)
        box.addWidget(self._selector)

        box.addWidget(label("Modelo de organização", "FieldLabel"))
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("AnalysisCombo")
        for model in MODELS.values():
            self._model_combo.addItem(f"{model.label}   ({model.description})", model.key)
        self._model_combo.setCurrentIndex(1)
        box.addWidget(self._model_combo)
        box.addWidget(
            label(
                f"Modelos B e C usam a {DATE_SOURCE}. Ela não é a data em que uma fotografia "
                "foi tirada (EXIF não é lido nesta etapa). Arquivos sem data válida vão para "
                "'Sem data' e ficam para revisão.",
                "MetricHint",
                wrap=True,
            )
        )

        box.addWidget(label("Destino simulado (não será criado)", "FieldLabel"))
        row = QHBoxLayout()
        self._destination = QLineEdit()
        self._destination.setObjectName("TextInput")
        self._destination.setPlaceholderText("Ex.: D:\\Organizado")
        self._destination.textChanged.connect(self._validate_destination)
        row.addWidget(self._destination, 1)
        choose = QPushButton("Escolher…")
        choose.clicked.connect(self._choose_destination)
        row.addWidget(choose)
        self._choose_button = choose
        box.addLayout(row)
        self._destination_alerts = label("", "BodyText", wrap=True)
        box.addWidget(self._destination_alerts)

        self._exclude_sensitive = QCheckBox(
            "Excluir da proposta as estruturas sensíveis (projetos, ambientes, aplicativos, "
            "bancos de dados e produtos Sysvar) — recomendado"
        )
        self._exclude_sensitive.setChecked(True)
        box.addWidget(self._exclude_sensitive)

        buttons = QHBoxLayout()
        self._start_button = QPushButton("Gerar prévia")
        self._start_button.setObjectName("PrimaryButton")
        self._start_button.clicked.connect(self._start)
        buttons.addWidget(self._start_button)
        buttons.addStretch(1)
        box.addLayout(buttons)
        return frame

    def _build_execution(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Execução", "SectionTitle"))
        heading.addStretch(1)
        self._cancel_button = QPushButton("Cancelar")
        self._cancel_button.clicked.connect(self._cancel)
        heading.addWidget(self._cancel_button)
        box.addLayout(heading)
        self._progress = QProgressBar()
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFixedHeight(6)
        box.addWidget(self._progress)
        self._state_label = label("Aguardando início.", "BodyText", wrap=True)
        box.addWidget(self._state_label)
        self._current_label = ElidedLabel("", "MetricHint")
        box.addWidget(self._current_label)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self._processed_tile = MetricCard("ARQUIVOS PROCESSADOS", "Tile", "TileValue")
        self._time_tile = MetricCard("TEMPO DECORRIDO", "Tile", "TileValue")
        for column, tile in enumerate((self._processed_tile, self._time_tile)):
            tile.set_value("—")
            grid.addWidget(tile, 0, column)
            grid.setColumnStretch(column, 1)
        box.addLayout(grid)
        return frame

    def _build_summary(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Resumo da prévia", "SectionTitle"))
        self._summary_caption = label("", "MetricHint", wrap=True)
        box.addWidget(self._summary_caption)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self._files_tile = MetricCard("ARQUIVOS CLASSIFICADOS", "Tile", "TileValue")
        self._volume_tile = MetricCard("VOLUME LÓGICO ENVOLVIDO", "Tile", "TileValue")
        self._folders_tile = MetricCard("PASTAS PROPOSTAS", "Tile", "TileValue")
        self._ok_tile = MetricCard("DESTINO VÁLIDO", "Tile", "TileValue")
        self._review_tile = MetricCard("REQUEREM REVISÃO", "Tile", "TileValue")
        self._conflict_tile = MetricCard("CONFLITOS — REQUER DECISÃO", "Tile", "TileValue")
        self._invalid_tile = MetricCard("CAMINHOS INVÁLIDOS", "Tile", "TileValue")
        self._already_tile = MetricCard("JÁ NO DESTINO", "Tile", "TileValue")
        self._excluded_tile = MetricCard("EXCLUÍDOS (ESTRUTURAS SENSÍVEIS)", "Tile", "TileValue")
        self._summary_tiles = (
            self._files_tile,
            self._volume_tile,
            self._folders_tile,
            self._ok_tile,
            self._review_tile,
            self._conflict_tile,
            self._invalid_tile,
            self._already_tile,
            self._excluded_tile,
        )
        for index, tile in enumerate(self._summary_tiles):
            grid.addWidget(tile, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        box.addLayout(grid)

        self._alerts_label = label("", "BodyText", wrap=True)
        box.addWidget(self._alerts_label)
        box.addWidget(label("Quantidade por categoria (tipo aparente pela extensão)", "FieldLabel"))
        self._categories_table = result_table(["Categoria", "Arquivos", "Volume"], (0,))
        self._categories_table.setMinimumHeight(150)
        box.addWidget(self._categories_table)
        box.addWidget(label("Estruturas sensíveis identificadas", "FieldLabel"))
        self._structures_table = result_table(["Tipo", "Pasta", "Arquivos", "Evidência"], (1, 3))
        self._structures_table.setMinimumHeight(120)
        box.addWidget(self._structures_table)
        return frame

    def _build_preview(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Prévia por arquivo", "SectionTitle"))
        row = QHBoxLayout()
        self._category_filter = QComboBox()
        self._category_filter.setObjectName("AnalysisCombo")
        self._category_filter.addItem("Todas as categorias", None)
        for key, (text, _folder) in CATEGORY_LABELS.items():
            self._category_filter.addItem(text, key)
        self._status_filter = QComboBox()
        self._status_filter.setObjectName("AnalysisCombo")
        self._status_filter.addItem("Todas as situações", None)
        for key, text in ITEM_LABELS.items():
            self._status_filter.addItem(text, key)
        self._search = QLineEdit()
        self._search.setObjectName("TextInput")
        self._search.setPlaceholderText("Pesquisar pelo nome do arquivo")
        self._search.returnPressed.connect(lambda: self._load_items(0))
        search_button = QPushButton("Pesquisar")
        search_button.clicked.connect(lambda: self._load_items(0))
        self._category_filter.currentIndexChanged.connect(lambda *_: self._load_items(0))
        self._status_filter.currentIndexChanged.connect(lambda *_: self._load_items(0))
        row.addWidget(self._category_filter)
        row.addWidget(self._status_filter)
        row.addWidget(self._search, 1)
        row.addWidget(search_button)
        box.addLayout(row)

        self._items_table = result_table(
            ["Nome", "Categoria", "Origem", "Destino proposto", "Situação"], (2, 3)
        )
        self._items_table.setMinimumHeight(260)
        self._items_table.itemSelectionChanged.connect(self._on_item_selected)
        box.addWidget(self._items_table)
        self._items_pager = Pager()
        self._items_pager.page_changed.connect(self._load_items)
        box.addWidget(self._items_pager)

        box.addSpacing(6)
        box.addWidget(label("Detalhes do arquivo selecionado", "SectionTitle"))
        details = QGridLayout()
        details.setHorizontalSpacing(24)
        details.setVerticalSpacing(6)
        details.setColumnStretch(1, 1)
        self._details: dict[str, QWidget] = {}
        for index, (key, title) in enumerate(
            (
                ("current", "Caminho atual"),
                ("proposed", "Destino proposto"),
                ("category", "Categoria"),
                ("justification", "Justificativa"),
                ("rule", "Regra aplicada"),
                ("date", "Data usada"),
                ("status", "Situação"),
                ("issues", "Alertas"),
            )
        ):
            details.addWidget(label(title, "FieldLabel"), index, 0, Qt.AlignmentFlag.AlignTop)
            value = label("—", "FieldValue", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.addWidget(value, index, 1)
            self._details[key] = value
        box.addLayout(details)
        return frame

    def _build_tree(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Estrutura de destino proposta (resumo)", "SectionTitle"))
        box.addWidget(
            label(
                "Pastas que seriam usadas no destino simulado, com a quantidade de arquivos "
                "propostos para cada uma. Nenhuma delas é criada.",
                "MetricHint",
                wrap=True,
            )
        )
        self._tree_table = result_table(["Pasta proposta", "Arquivos", "Volume"], (0,))
        self._tree_table.setMinimumHeight(200)
        box.addWidget(self._tree_table)
        self._tree_pager = Pager()
        self._tree_pager.page_changed.connect(self._load_tree)
        box.addWidget(self._tree_pager)
        return frame

    # Configuração --------------------------------------------------------

    def reload_analyses(self) -> None:
        if self.is_running():
            return
        analyses = []
        repo = self._context.duplicates
        if repo is not None:
            try:
                analyses = repo.completed_analyses()
            except DatabaseError:
                logger.exception("Falha ao listar as análises concluídas")
        self._selector.set_analyses(analyses)
        self._update_controls()

    def _choose_destination(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Escolher pasta de destino simulado", "", QFileDialog.Option.ShowDirsOnly
        )
        if path:
            self._destination.setText(normalize_location(path))

    def _validate_destination(self, *_args: object) -> None:
        if not hasattr(self, "_destination_alerts"):
            return
        chosen = self._selector.current_set()
        text = self._destination.text().strip()
        if chosen is None:
            self._destination_ok = False
            self._destination_alerts.setText("")
        else:
            alerts, usable = destination_alerts(chosen.root, text)
            self._destination_ok = usable
            self._destination_alerts.setText("\n".join(f"• {alert}" for alert in alerts))
        self._update_controls()

    # Execução ------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        self._busy_check = check
        # Revisão de operações: nada físico enquanto outra tarefa (ou a própria
        # simulação) estiver em andamento.
        self._review.set_busy_check(lambda: check() or self._simulation_running())

    def _simulation_running(self) -> bool:
        return self._thread is not None

    def is_running(self) -> bool:
        """Simulação OU operação física (revisão de operações) em andamento."""
        review = getattr(self, "_review", None)
        return self._simulation_running() or (review is not None and review.is_running())

    def _start(self) -> None:
        database = self._context.database
        chosen = self._selector.current_set()
        if self.is_running() or database is None or chosen is None or not self._destination_ok:
            return
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self, APP_NAME, "Há outra tarefa em andamento. Aguarde a conclusão para iniciar."
            )
            return
        request = OrganizationRequest(
            analysis_id=chosen.analysis_id,
            root=chosen.root,
            model=self._model_combo.currentData(),
            destination=normalize_location(self._destination.text().strip()),
            exclude_sensitive=self._exclude_sensitive.isChecked(),
        )
        self._cancelling = False
        self._worker = OrganizationWorker(database, request)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._on_thread_finished)

        self._show_simulation(None, "Simulação em andamento.")
        self._processed_tile.set_value("—")
        self._time_tile.set_value("0 s")
        self._state_label.setText("Lendo os metadados indexados e identificando estruturas…")
        self._current_label.set_full_text("")
        self._progress.setRange(0, 0)
        self._elapsed.start()
        self._clock.start()
        self._update_controls()
        self._thread.start()

    def _cancel(self) -> None:
        if self._worker is not None and not self._cancelling:
            self._cancelling = True
            self._worker.cancel()
            self._state_label.setText("Cancelando…")
            self._update_controls()

    def _on_progress(self, progress: OrganizationProgress) -> None:
        if progress.total > 0 and progress.processed > 0:
            self._progress.setRange(0, progress.total)
            self._progress.setValue(progress.processed)
            if not self._cancelling:
                self._state_label.setText("Classificando e calculando caminhos propostos…")
        self._processed_tile.set_value(
            format_count(progress.processed), f"de {format_count(progress.total)}"
        )
        self._current_label.set_full_text(
            f"Classificando: {progress.current_path}" if progress.current_path else ""
        )

    def _on_finished(self, outcome: OrganizationOutcome) -> None:
        self._clock.stop()
        self._time_tile.set_value(format_duration(outcome.elapsed_seconds))
        self._current_label.set_full_text("")
        self._progress.setRange(0, 1)
        self._progress.setValue(1 if outcome.status == STATUS_COMPLETED else 0)
        if outcome.status == STATUS_COMPLETED and outcome.simulation_id is not None:
            self._state_label.setText(
                "Prévia concluída. Nenhuma alteração foi feita no disco."
            )
            self._show_simulation(
                self._load_simulation(outcome.simulation_id), "Prévia gerada agora"
            )
        else:
            if outcome.status == STATUS_CANCELLED:
                message = "Simulação cancelada. Resultados parciais não são apresentados."
            else:
                message = outcome.error_message or "A simulação falhou."
            self._state_label.setText(message)
            self._show_simulation(
                None, f"{status_label(outcome.status)}: nenhum resultado apresentado."
            )
        self.simulation_finished.emit()

    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._cancelling = False
        self._update_controls()

    def _tick(self) -> None:
        self._time_tile.set_value(format_duration(self._elapsed.elapsed() / 1000))

    def request_shutdown(self) -> bool:
        if self._review.is_running():
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "Uma operação com arquivos está em andamento. Deseja cancelá-la com segurança e "
                "sair? O arquivo em gravação é descartado e nenhuma unidade fica separada.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes or not self._review.request_shutdown():
                return False
        if not self._simulation_running():
            return True
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Uma simulação de organização está em andamento. Deseja cancelá-la e sair?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and not self._thread.wait(15000):
            logger.warning("A simulação não terminou no tempo de espera do encerramento")
        return True

    # Resultados ----------------------------------------------------------

    def _load_simulation(self, simulation_id: int) -> SimulationRecord | None:
        repo = self._context.organizations
        if repo is None:
            return None
        try:
            return repo.simulation(simulation_id)
        except DatabaseError:
            logger.exception("Falha ao consultar a simulação")
            return None

    def _show_latest_completed(self) -> None:
        repo = self._context.organizations
        record = None
        if repo is not None:
            try:
                record = repo.latest_completed()
            except DatabaseError:
                logger.exception("Falha ao consultar a última simulação")
        if record is None:
            self._show_simulation(None, "Nenhuma simulação de organização concluída até o momento.")
        else:
            self._show_simulation(record, "Última prévia concluída")

    def _show_simulation(self, record: SimulationRecord | None, title: str) -> None:
        self._simulation = record
        self._review.set_simulation(
            record if record is not None and record.status == STATUS_COMPLETED else None
        )
        for table in (self._categories_table, self._structures_table, self._items_table, self._tree_table):
            table.setRowCount(0)
        self._clear_details()
        if record is None:
            self._summary_caption.setText(title)
            for tile in self._summary_tiles:
                tile.set_value("—")
            self._alerts_label.setText("")
            self._items_pager.set_total(0, PAGE_SIZE)
            self._tree_pager.set_total(0, PAGE_SIZE)
            return
        r = record
        model = MODELS.get(r.model)
        self._summary_caption.setText(
            f"{title}: {format_timestamp(r.started_at)}  ·  "
            f"{model.label if model else r.model}\nOrigem: {r.root}\nDestino simulado: "
            f"{r.destination}  ·  estruturas sensíveis "
            f"{'excluídas da proposta' if r.exclude_sensitive else 'mantidas para revisão'}"
        )
        self._files_tile.set_value(format_count(r.files_count))
        self._volume_tile.set_value(
            format_bytes(r.proposal_bytes),
            "Volume que seria reorganizado. Não é espaço recuperável.",
        )
        self._folders_tile.set_value(format_count(r.folders_count), "Incluindo pastas intermediárias")
        self._ok_tile.set_value(format_count(r.ok_count))
        self._review_tile.set_value(
            format_count(r.review_count),
            "Ambíguos, desconhecidos, sem extensão, sem data ou sensíveis",
        )
        self._conflict_tile.set_value(
            format_count(r.conflict_count), "Arquivos diferentes com o mesmo caminho proposto"
        )
        self._invalid_tile.set_value(
            format_count(r.invalid_count), "Nome reservado/inválido ou caminho longo no Windows"
        )
        self._already_tile.set_value(format_count(r.already_count))
        self._excluded_tile.set_value(
            format_count(r.excluded_count), f"{format_count(r.structures_count)} estrutura(s)"
        )
        try:
            alerts = json.loads(r.alerts or "[]")
        except ValueError:
            alerts = []
        self._alerts_label.setText(
            "Alertas do destino:\n" + "\n".join(f"• {a}" for a in alerts) if alerts else ""
        )

        repo = self._context.organizations
        if repo is None:
            return
        try:
            categories = repo.category_counts(r.id)
            structures = repo.structures(r.id)
        except DatabaseError:
            logger.exception("Falha ao consultar o resumo da simulação")
            categories, structures = [], []
        self._categories_table.setRowCount(len(categories))
        for row, (category, count, size) in enumerate(categories):
            self._categories_table.setItem(row, 0, _item(category_label(category)))
            self._categories_table.setItem(row, 1, _item(format_count(count), right=True))
            self._categories_table.setItem(row, 2, _item(format_bytes(size), right=True))
        self._structures_table.setRowCount(len(structures))
        for row, (path, kind, reason, count) in enumerate(structures):
            self._structures_table.setItem(row, 0, _item(KIND_LABELS.get(kind, kind)))
            self._structures_table.setItem(row, 1, _item(path, tooltip=path))
            self._structures_table.setItem(row, 2, _item(format_count(count), right=True))
            self._structures_table.setItem(row, 3, _item(reason, tooltip=reason))
        self._load_items(0)
        self._load_tree(0)

    def _load_items(self, page: int) -> None:
        table = self._items_table
        table.setRowCount(0)
        self._rows = []
        self._clear_details()
        record, repo = self._simulation, self._context.organizations
        if record is None or repo is None:
            return
        category = self._category_filter.currentData()
        status = self._status_filter.currentData()
        search = self._search.text().strip() or None
        try:
            total = repo.count_items(record.id, category, status, search)
            self._items_pager.set_total(total, PAGE_SIZE, page)
            self._rows = repo.items(
                record.id, category, status, search, self._items_pager.page * PAGE_SIZE, PAGE_SIZE
            )
        except DatabaseError:
            logger.exception("Falha ao consultar a prévia")
            return
        table.setRowCount(len(self._rows))
        for index, item in enumerate(self._rows):
            proposed = item.proposed_path or "— (mantido no lugar; excluído da proposta)"
            table.setItem(index, 0, _item(item.name, tooltip=item.full_path))
            table.setItem(index, 1, _item(category_label(item.category)))
            table.setItem(index, 2, _item(item.folder, tooltip=item.folder))
            table.setItem(index, 3, _item(proposed, tooltip=proposed))
            table.setItem(index, 4, _item(ITEM_LABELS.get(item.status, item.status)))

    def _on_item_selected(self) -> None:
        index = self._items_table.currentRow()
        if not 0 <= index < len(self._rows):
            self._clear_details()
            return
        item = self._rows[index]
        if item.year is None:
            date_text = (
                f"Não usada pelo modelo (modificado em {format_epoch(item.modified_at)})"
                if self._simulation and self._simulation.model == "A"
                else f"Sem data válida ({DATE_SOURCE}: {format_epoch(item.modified_at)})"
            )
        else:
            month = f"/{item.month:02d}" if item.month else ""
            date_text = f"{item.year}{month} — {DATE_SOURCE}"
        values = {
            "current": item.full_path,
            "proposed": item.proposed_path or "Nenhum: arquivo excluído da proposta.",
            "category": category_label(item.category)
            + ("  (extensão ambígua)" if item.ambiguous else ""),
            "justification": item.justification,
            "rule": item.rule,
            "date": date_text,
            "status": ITEM_LABELS.get(item.status, item.status),
            "issues": ", ".join(ISSUE_LABELS.get(code, code) for code in item.issues) or "Nenhum",
        }
        for key, text in values.items():
            self._details[key].setText(text)

    def _clear_details(self) -> None:
        for value in getattr(self, "_details", {}).values():
            value.setText("—")

    def _load_tree(self, page: int) -> None:
        table = self._tree_table
        table.setRowCount(0)
        record, repo = self._simulation, self._context.organizations
        if record is None or repo is None:
            return
        try:
            total = repo.count_folders(record.id)
            self._tree_pager.set_total(total, PAGE_SIZE, page)
            rows = repo.folders(record.id, self._tree_pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar a estrutura proposta")
            return
        table.setRowCount(len(rows))
        for index, (folder, count, size) in enumerate(rows):
            table.setItem(index, 0, _item(folder, tooltip=folder))
            table.setItem(index, 1, _item(format_count(count), right=True))
            table.setItem(index, 2, _item(format_bytes(size), right=True))

    def _update_controls(self) -> None:
        if not hasattr(self, "_start_button"):
            return
        running = self.is_running()
        self._selector.set_controls_enabled(not running)
        for widget in (
            self._model_combo,
            self._destination,
            self._choose_button,
            self._exclude_sensitive,
        ):
            widget.setEnabled(not running)
        self._start_button.setEnabled(
            not running
            and self._context.database_ok
            and self._selector.current_set() is not None
            and self._destination_ok
        )
        self._cancel_button.setEnabled(self._simulation_running() and not self._cancelling)
        if hasattr(self, "_review"):
            self._review.set_locked(self._simulation_running())
