"""Página Comparar Pastas: comparação informativa entre dois conjuntos indexados.

Somente consulta: não há controles de sincronização, cópia, movimentação ou
exclusão.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.comparison.folders import (
    same_set,
    sets_overlap,
    summarize_set,
)
from sysvar_organizer.engines.comparison.models import (
    ComparisonOutcome,
    ComparisonProgress,
    ComparisonSet,
)
from sysvar_organizer.engines.file_analysis.locations import (
    is_same_or_inside,
    normalize_location,
)
from sysvar_organizer.engines.file_analysis.models import STATUS_CANCELLED, STATUS_COMPLETED
from sysvar_organizer.persistence.comparison_repository import ComparisonRecord, ResultRow
from sysvar_organizer.persistence.duplicate_repository import SourceAnalysis
from sysvar_organizer.ui.comparison_worker import ComparisonWorker
from sysvar_organizer.ui.formatting import (
    format_bytes,
    format_count,
    format_duration,
    format_epoch,
    format_timestamp,
    status_label,
)
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

KIND_LABELS = {
    "identical": "Idêntico",
    "identical_renamed": "Idêntico, nome/local diferente",
    "different": "Diferente",
    "only_a": "Exclusivo de A",
    "only_b": "Exclusivo de B",
    "indeterminate": "Indeterminado",
}
KIND_FILTERS = [
    (None, "Todas as classificações"),
    ("identical", "Idênticos"),
    ("identical_renamed", "Idênticos com nomes diferentes"),
    ("different", "Diferentes"),
    ("only_a", "Exclusivos de A"),
    ("only_b", "Exclusivos de B"),
    ("indeterminate", "Indeterminados"),
]
FOLDER_LABELS = {"both": "Nos dois lados", "only_a": "Exclusiva de A", "only_b": "Exclusiva de B"}
FOLDER_FILTERS = [
    (None, "Todas as pastas"),
    ("both", "Nos dois lados"),
    ("only_a", "Exclusivas de A"),
    ("only_b", "Exclusivas de B"),
]


def _item(text: str, *, right: bool = False, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def describe(row: ResultRow) -> str:
    """Observação curta de uma linha de resultado."""
    physical = "  ·  mesmo arquivo físico (hard link ou sobreposição)" if row.same_physical else ""
    if row.kind == "identical":
        return "Mesmo caminho relativo" + physical
    if row.kind == "identical_renamed":
        if row.name_differs and row.folder_differs:
            text = "Nome e subpasta diferentes"
        elif row.name_differs:
            text = "Nome diferente"
        elif row.folder_differs:
            text = "Subpasta diferente"
        else:
            text = "Caminho diferente"
        return text + physical
    if row.kind == "different":
        if row.a and row.b and row.a.size_bytes != row.b.size_bytes:
            return "Mesmo caminho, tamanhos diferentes"
        return "Mesmo caminho, conteúdo diferente (SHA-256)"
    if row.kind == "only_a":
        return "Sem correspondência em B"
    if row.kind == "only_b":
        return "Sem correspondência em A"
    return row.detail or "Não foi possível comparar com segurança"


class SetSelector(QFrame):
    """Seleção de um conjunto: análise concluída + pasta indexada nela."""

    changed = Signal()

    def __init__(self, title: str, context: AppContext) -> None:
        super().__init__()
        self.setObjectName("Tile")
        self._context = context
        self._analyses: list[SourceAnalysis] = []
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 12, 14, 14)
        box.setSpacing(6)
        box.addWidget(label(title, "SectionTitle"))

        box.addWidget(label("Análise concluída", "FieldLabel"))
        self._analysis_combo = QComboBox()
        self._analysis_combo.setObjectName("AnalysisCombo")
        self._analysis_combo.currentIndexChanged.connect(self._on_analysis_changed)
        box.addWidget(self._analysis_combo)

        box.addWidget(label("Pasta", "FieldLabel"))
        row = QHBoxLayout()
        self._folder_combo = QComboBox()
        self._folder_combo.setObjectName("AnalysisCombo")
        self._folder_combo.currentIndexChanged.connect(self._on_folder_changed)
        row.addWidget(self._folder_combo, 1)
        self._subfolder_button = QPushButton("Escolher subpasta…")
        self._subfolder_button.clicked.connect(self._choose_subfolder)
        row.addWidget(self._subfolder_button)
        box.addLayout(row)

        self._path_label = label("", "FieldValue", wrap=True)
        self._path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self._path_label)
        self._info_label = label("", "MetricHint", wrap=True)
        box.addWidget(self._info_label)

    def set_analyses(self, analyses: list[SourceAnalysis]) -> None:
        current = self._analysis_combo.currentData()
        self._analyses = analyses
        self._analysis_combo.blockSignals(True)
        self._analysis_combo.clear()
        for analysis in analyses:
            self._analysis_combo.addItem(
                f"{format_timestamp(analysis.started_at)}   ·   {analysis.target_path}",
                analysis.id,
            )
        if current is not None:
            index = self._analysis_combo.findData(current)
            if index >= 0:
                self._analysis_combo.setCurrentIndex(index)
        self._analysis_combo.blockSignals(False)
        self._on_analysis_changed()

    def select_analysis_index(self, index: int) -> None:
        if 0 <= index < self._analysis_combo.count():
            self._analysis_combo.setCurrentIndex(index)

    def analysis(self) -> SourceAnalysis | None:
        analysis_id = self._analysis_combo.currentData()
        for analysis in self._analyses:
            if analysis.id == analysis_id:
                return analysis
        return None

    def current_set(self) -> ComparisonSet | None:
        analysis = self.analysis()
        root = self._folder_combo.currentData()
        if analysis is None or not root:
            return None
        return ComparisonSet(analysis.id, root)

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in (self._analysis_combo, self._folder_combo, self._subfolder_button):
            widget.setEnabled(enabled)

    def _on_analysis_changed(self, *_args: object) -> None:
        analysis = self.analysis()
        self._folder_combo.blockSignals(True)
        self._folder_combo.clear()
        if analysis is not None:
            for path in analysis.locations or (analysis.target_path,):
                self._folder_combo.addItem(path, path)
        self._folder_combo.blockSignals(False)
        self._on_folder_changed()

    def _on_folder_changed(self, *_args: object) -> None:
        analysis = self.analysis()
        chosen = self.current_set()
        if analysis is None or chosen is None:
            self._path_label.setText("Nenhuma análise concluída disponível.")
            self._info_label.setText("")
        else:
            self._path_label.setText(chosen.root)
            files = "—"
            repo = self._context.comparisons
            if repo is not None:
                try:
                    summary = summarize_set(repo, analysis.id, chosen.root)
                    files = (
                        f"{format_count(summary.files)} arquivos indexados  ·  "
                        f"{format_bytes(summary.total_bytes)}"
                    )
                except DatabaseError:
                    logger.exception("Falha ao resumir o conjunto")
            self._info_label.setText(
                f"Análise de {format_timestamp(analysis.started_at)}  ·  {files}"
            )
        self.changed.emit()

    def _choose_subfolder(self) -> None:
        analysis = self.analysis()
        chosen = self.current_set()
        if analysis is None or chosen is None:
            return
        path = QFileDialog.getExistingDirectory(self, "Escolher subpasta do conjunto", chosen.root)
        if not path:
            return
        path = normalize_location(path)
        if not any(is_same_or_inside(path, loc) for loc in analysis.locations):
            QMessageBox.information(
                self,
                APP_NAME,
                "A pasta escolhida não pertence aos locais examinados nessa análise.",
            )
            return
        repo = self._context.comparisons
        try:
            indexed = repo is not None and summarize_set(repo, analysis.id, path).indexed
        except DatabaseError:
            indexed = False
        if not indexed:
            QMessageBox.information(
                self, APP_NAME, "A pasta escolhida não foi indexada nessa análise."
            )
            return
        index = self._folder_combo.findData(path)
        if index < 0:
            self._folder_combo.addItem(path, path)
            index = self._folder_combo.count() - 1
        self._folder_combo.setCurrentIndex(index)


class ComparePage(QWidget):
    comparison_finished = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._thread: QThread | None = None
        self._worker: ComparisonWorker | None = None
        self._cancelling = False
        self._busy_check: Callable[[], bool] | None = None
        self._comparison: ComparisonRecord | None = None
        self._rows: list[ResultRow] = []
        self._elapsed = QElapsedTimer()
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick)

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Comparar Pastas",
                "Compara duas pastas já indexadas pelo conteúdo (SHA-256), pelo caminho "
                "relativo e pela estrutura de subpastas. Comparação apenas informativa: "
                "nenhum arquivo é copiado, sincronizado, movido ou excluído.",
                badge("Somente leitura", "ok"),
            )
        )
        layout.addWidget(self._build_selection())
        layout.addWidget(self._build_execution())
        layout.addWidget(self._build_results())
        layout.addWidget(self._build_structure())
        layout.addWidget(self._build_listing())
        layout.addStretch(1)

        self.reload_analyses()
        self._show_latest_completed()
        self._update_controls()

    # Construção ----------------------------------------------------------

    def _build_selection(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Seleção dos conjuntos", "SectionTitle"))
        box.addWidget(
            label(
                "Escolha, para cada lado, uma análise de armazenamento concluída e uma pasta "
                "indexada nela (o local examinado ou uma subpasta).",
                "MetricHint",
                wrap=True,
            )
        )
        row = QHBoxLayout()
        row.setSpacing(12)
        self._selector_a = SetSelector("Origem A", self._context)
        self._selector_b = SetSelector("Origem B", self._context)
        self._selector_a.changed.connect(self._update_controls)
        self._selector_b.changed.connect(self._update_controls)
        row.addWidget(self._selector_a, 1)
        row.addWidget(self._selector_b, 1)
        box.addLayout(row)

        self._selection_warning = label("", "BodyText", wrap=True)
        box.addWidget(self._selection_warning)
        buttons = QHBoxLayout()
        self._start_button = QPushButton("Iniciar comparação")
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
        self._read_tile = MetricCard("DADOS LIDOS", "Tile", "TileValue")
        self._time_tile = MetricCard("TEMPO DECORRIDO", "Tile", "TileValue")
        for column, tile in enumerate((self._processed_tile, self._read_tile, self._time_tile)):
            tile.set_value("—")
            grid.addWidget(tile, 0, column)
            grid.setColumnStretch(column, 1)
        box.addSpacing(4)
        box.addLayout(grid)
        return frame

    def _build_results(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Resultados", "SectionTitle"))
        self._results_caption = label("", "MetricHint", wrap=True)
        box.addWidget(self._results_caption)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self._identical_tile = MetricCard("IDÊNTICOS", "Tile", "TileValue")
        self._renamed_tile = MetricCard("IDÊNTICOS COM NOMES DIFERENTES", "Tile", "TileValue")
        self._different_tile = MetricCard("DIFERENTES", "Tile", "TileValue")
        self._indeterminate_tile = MetricCard("INDETERMINADOS", "Tile", "TileValue")
        self._only_a_tile = MetricCard("EXCLUSIVOS DE A", "Tile", "TileValue")
        self._only_b_tile = MetricCard("EXCLUSIVOS DE B", "Tile", "TileValue")
        self._shared_tile = MetricCard("VOLUME LÓGICO COMPARTILHADO", "Tile", "TileValue")
        self._physical_tile = MetricCard("ARQUIVOS FÍSICOS DISTINTOS", "Tile", "TileValue")
        self._result_tiles = (
            self._identical_tile,
            self._renamed_tile,
            self._different_tile,
            self._indeterminate_tile,
            self._only_a_tile,
            self._only_b_tile,
            self._shared_tile,
            self._physical_tile,
        )
        for index, tile in enumerate(self._result_tiles):
            grid.addWidget(tile, index // 4, index % 4)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        box.addSpacing(4)
        box.addLayout(grid)
        return frame

    def _build_structure(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Comparação estrutural", "SectionTitle"))
        self._equivalence_label = label("", "BodyText", wrap=True)
        box.addWidget(self._equivalence_label)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self._folders_both_tile = MetricCard("PASTAS NOS DOIS LADOS", "Tile", "TileValue")
        self._folders_a_tile = MetricCard("PASTAS EXCLUSIVAS DE A", "Tile", "TileValue")
        self._folders_b_tile = MetricCard("PASTAS EXCLUSIVAS DE B", "Tile", "TileValue")
        self._moved_tile = MetricCard("CONTEÚDO IGUAL EM SUBPASTAS DIFERENTES", "Tile", "TileValue")
        self._structure_tiles = (
            self._folders_both_tile,
            self._folders_a_tile,
            self._folders_b_tile,
            self._moved_tile,
        )
        for column, tile in enumerate(self._structure_tiles):
            grid.addWidget(tile, 0, column)
            grid.setColumnStretch(column, 1)
        box.addLayout(grid)

        row = QHBoxLayout()
        self._folder_filter = QComboBox()
        self._folder_filter.setObjectName("AnalysisCombo")
        for value, text in FOLDER_FILTERS:
            self._folder_filter.addItem(text, value)
        self._folder_filter.currentIndexChanged.connect(lambda *_: self._load_folders(0))
        row.addWidget(self._folder_filter)
        row.addStretch(1)
        box.addLayout(row)
        self._folders_table = result_table(["Situação", "Pasta (caminho relativo)"], (1,))
        self._folders_table.setMinimumHeight(160)
        box.addWidget(self._folders_table)
        self._folders_pager = Pager()
        self._folders_pager.page_changed.connect(self._load_folders)
        box.addWidget(self._folders_pager)
        return frame

    def _build_listing(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Correspondências", "SectionTitle"))
        row = QHBoxLayout()
        self._kind_filter = QComboBox()
        self._kind_filter.setObjectName("AnalysisCombo")
        for value, text in KIND_FILTERS:
            self._kind_filter.addItem(text, value)
        self._kind_filter.currentIndexChanged.connect(lambda *_: self._load_results(0))
        row.addWidget(self._kind_filter)
        row.addStretch(1)
        box.addLayout(row)
        self._listing_caption = label("", "MetricHint", wrap=True)
        box.addWidget(self._listing_caption)

        self._results_table = result_table(
            ["Classificação", "Arquivo A", "Arquivo B", "Tamanho", "Observação"], (1, 2)
        )
        self._results_table.setMinimumHeight(260)
        self._results_table.itemSelectionChanged.connect(self._on_result_selected)
        box.addWidget(self._results_table)
        self._results_pager = Pager()
        self._results_pager.page_changed.connect(self._load_results)
        box.addWidget(self._results_pager)

        box.addSpacing(6)
        box.addWidget(label("Detalhes da correspondência selecionada", "SectionTitle"))
        details = QGridLayout()
        details.setHorizontalSpacing(24)
        details.setVerticalSpacing(6)
        details.setColumnStretch(1, 1)
        self._detail_values: dict[str, QWidget] = {}
        for index, (key, title) in enumerate(
            (
                ("kind", "Classificação"),
                ("note", "Observação"),
                ("a_path", "Arquivo A"),
                ("a_meta", "Tamanho e data (A)"),
                ("a_sha", "SHA-256 (A)"),
                ("b_path", "Arquivo B"),
                ("b_meta", "Tamanho e data (B)"),
                ("b_sha", "SHA-256 (B)"),
            )
        ):
            details.addWidget(label(title, "FieldLabel"), index, 0, Qt.AlignmentFlag.AlignTop)
            value = label("—", "FieldValue", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.addWidget(value, index, 1)
            self._detail_values[key] = value
        box.addLayout(details)
        return frame

    # Seleção -------------------------------------------------------------

    def reload_analyses(self) -> None:
        if self.is_running():
            return
        analyses: list[SourceAnalysis] = []
        repo = self._context.duplicates
        if repo is not None:
            try:
                analyses = repo.completed_analyses()
            except DatabaseError:
                logger.exception("Falha ao listar as análises concluídas")
        first_load = self._selector_b.analysis() is None
        self._selector_a.set_analyses(analyses)
        self._selector_b.set_analyses(analyses)
        if first_load and len(analyses) > 1:
            # Sugestão inicial: as duas análises mais recentes, uma em cada lado.
            self._selector_a.select_analysis_index(1)
            self._selector_b.select_analysis_index(0)
        self._update_controls()

    def _selection_state(self) -> tuple[ComparisonSet | None, ComparisonSet | None, str, bool]:
        set_a = self._selector_a.current_set()
        set_b = self._selector_b.current_set()
        if set_a is None or set_b is None:
            return set_a, set_b, "Selecione as duas origens para comparar.", False
        if same_set(set_a, set_b):
            return set_a, set_b, "A e B são o mesmo conjunto. Escolha conjuntos diferentes.", False
        if sets_overlap(set_a, set_b):
            return (
                set_a,
                set_b,
                "Atenção: as duas pastas se sobrepõem fisicamente (uma contém a outra). "
                "Os arquivos em comum serão o mesmo arquivo físico nos dois lados.",
                True,
            )
        return set_a, set_b, "", True

    # Execução ------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        """Função que indica outra tarefa em andamento."""
        self._busy_check = check

    def is_running(self) -> bool:
        return self._thread is not None

    def _start(self) -> None:
        database = self._context.database
        set_a, set_b, warning, allowed = self._selection_state()
        if self.is_running() or database is None or not allowed or set_a is None or set_b is None:
            return
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self,
                APP_NAME,
                "Há outra tarefa em andamento (análise de armazenamento ou busca de "
                "duplicados). Aguarde a conclusão para comparar pastas.",
            )
            return
        if warning:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"{warning}\n\nDeseja comparar mesmo assim?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._cancelling = False
        self._worker = ComparisonWorker(database, set_a, set_b)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._on_thread_finished)

        self._show_record(None, "Comparação em andamento.")
        self._processed_tile.set_value("—")
        self._read_tile.set_value("—")
        self._time_tile.set_value("0 s")
        self._state_label.setText("Selecionando os arquivos dos conjuntos…")
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
            self._state_label.setText("Cancelando… aguardando a conclusão da leitura atual.")
            self._update_controls()

    def _on_progress(self, progress: ComparisonProgress) -> None:
        if progress.total > 0:
            self._progress.setRange(0, progress.total)
            self._progress.setValue(progress.processed)
            if not self._cancelling:
                self._state_label.setText("Revalidando arquivos e calculando SHA-256…")
        self._processed_tile.set_value(
            format_count(progress.processed), f"de {format_count(progress.total)}"
        )
        self._read_tile.set_value(
            format_bytes(progress.bytes_read),
            f"{format_count(progress.read_count)} calculados · "
            f"{format_count(progress.reused)} reaproveitados",
        )
        self._current_label.set_full_text(
            f"Examinando: {progress.current_path}" if progress.current_path else ""
        )

    def _on_finished(self, outcome: ComparisonOutcome) -> None:
        self._clock.stop()
        self._time_tile.set_value(format_duration(outcome.elapsed_seconds))
        self._current_label.set_full_text("")
        self._progress.setRange(0, 1)
        self._progress.setValue(1 if outcome.status == STATUS_COMPLETED else 0)
        if outcome.status == STATUS_COMPLETED and outcome.comparison_id is not None:
            self._state_label.setText("Comparação concluída.")
            record = self._load_record(outcome.comparison_id)
            self._show_record(record, "Resultado da comparação executada agora")
        else:
            if outcome.status == STATUS_CANCELLED:
                message = "Comparação cancelada. Resultados parciais não são apresentados."
            else:
                message = outcome.error_message or "A comparação falhou."
            self._state_label.setText(message)
            self._show_record(None, f"{status_label(outcome.status)}: nenhum resultado apresentado.")
        self.comparison_finished.emit()

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
        """Chamado ao fechar a janela. Retorna False se o usuário preferir continuar."""
        if not self.is_running():
            return True
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Uma comparação de pastas está em andamento. Deseja cancelá-la e sair?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and not self._thread.wait(15000):
            logger.warning("A comparação não terminou no tempo de espera do encerramento")
        return True

    # Resultados ----------------------------------------------------------

    def _load_record(self, comparison_id: int) -> ComparisonRecord | None:
        repo = self._context.comparisons
        if repo is None:
            return None
        try:
            return repo.comparison(comparison_id)
        except DatabaseError:
            logger.exception("Falha ao consultar a comparação")
            return None

    def _show_latest_completed(self) -> None:
        repo = self._context.comparisons
        record = None
        if repo is not None:
            try:
                record = repo.latest_completed()
            except DatabaseError:
                logger.exception("Falha ao consultar a última comparação")
        if record is None:
            self._show_record(None, "Nenhuma comparação concluída até o momento.")
        else:
            self._show_record(record, "Última comparação concluída")

    def _show_record(self, record: ComparisonRecord | None, title: str) -> None:
        self._comparison = record
        if record is None:
            self._results_caption.setText(title)
            for tile in (*self._result_tiles, *self._structure_tiles):
                tile.set_value("—")
            self._equivalence_label.setText("")
            self._folders_table.setRowCount(0)
            self._folders_pager.set_total(0, PAGE_SIZE)
            self._results_table.setRowCount(0)
            self._results_pager.set_total(0, PAGE_SIZE)
            self._listing_caption.setText("")
            self._clear_details()
            return

        r = record
        self._results_caption.setText(
            f"{title}: {format_timestamp(r.started_at)}\n"
            f"A: {r.root_a}  ({format_count(r.files_a)} arquivos)\n"
            f"B: {r.root_b}  ({format_count(r.files_b)} arquivos)"
        )
        self._identical_tile.set_value(
            format_count(r.identical_pairs),
            f"Mesmo caminho e conteúdo  ·  {format_count(r.identical_files_a)} arquivos em A, "
            f"{format_count(r.identical_files_b)} em B",
        )
        renamed_hint = (
            f"{format_count(r.renamed_files_a)} arquivos em A, "
            f"{format_count(r.renamed_files_b)} em B (correspondências)"
        )
        if (r.renamed_pairs or 0) > (r.renamed_listed or 0):
            renamed_hint += f"  ·  {format_count(r.renamed_listed)} listadas"
        self._renamed_tile.set_value(format_count(r.renamed_pairs), renamed_hint)
        self._different_tile.set_value(
            format_count(r.different_count), "Mesmo caminho, conteúdo diferente"
        )
        self._indeterminate_tile.set_value(
            format_count(r.indeterminate_count), "Não comparados com segurança"
        )
        self._only_a_tile.set_value(format_count(r.only_a_count), "Sem correspondência em B")
        self._only_b_tile.set_value(format_count(r.only_b_count), "Sem correspondência em A")
        self._shared_tile.set_value(
            format_bytes(r.shared_bytes),
            f"{format_count(r.shared_contents)} conteúdos distintos presentes nos dois lados",
        )
        self._physical_tile.set_value(
            format_count(r.matched_physical),
            f"Nas correspondências de conteúdo ({format_count(r.matched_files_a)} em A + "
            f"{format_count(r.matched_files_b)} em B)  ·  {format_count(r.same_physical_pairs)} "
            "par(es) do mesmo arquivo físico",
        )
        self._folders_both_tile.set_value(format_count(r.folders_both))
        self._folders_a_tile.set_value(format_count(r.folders_only_a))
        self._folders_b_tile.set_value(format_count(r.folders_only_b))
        self._moved_tile.set_value(format_count(r.moved_pairs), "Correspondências listadas")
        yes_no = {1: "sim", 0: "não"}
        self._equivalence_label.setText(
            f"Equivalência de conteúdo: {yes_no.get(r.content_equivalent, '—')} "
            "(todo conteúdo de cada lado existe no outro).\n"
            f"Equivalência de estrutura: {yes_no.get(r.structure_equivalent, '—')} "
            "(mesmas pastas e cada arquivo idêntico no mesmo caminho relativo)."
        )
        self._load_folders(0)
        self._load_results(0)

    def _load_folders(self, page: int) -> None:
        table = self._folders_table
        table.setRowCount(0)
        record, repo = self._comparison, self._context.comparisons
        if record is None or repo is None:
            return
        status = self._folder_filter.currentData()
        try:
            total = repo.count_folders(record.id, status)
            self._folders_pager.set_total(total, PAGE_SIZE, page)
            rows = repo.folders(record.id, status, self._folders_pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar as pastas da comparação")
            return
        table.setRowCount(len(rows))
        for index, (rel_path, folder_status) in enumerate(rows):
            table.setItem(index, 0, _item(FOLDER_LABELS.get(folder_status, folder_status)))
            table.setItem(index, 1, _item(rel_path, tooltip=rel_path))

    def _load_results(self, page: int) -> None:
        table = self._results_table
        table.setRowCount(0)
        self._rows = []
        self._clear_details()
        record, repo = self._comparison, self._context.comparisons
        if record is None or repo is None:
            return
        kind = self._kind_filter.currentData()
        try:
            total = repo.count_results(record.id, kind)
            self._results_pager.set_total(total, PAGE_SIZE, page)
            self._rows = repo.results(
                record.id, kind, self._results_pager.page * PAGE_SIZE, PAGE_SIZE
            )
        except DatabaseError:
            logger.exception("Falha ao consultar os resultados da comparação")
            return

        omitted = (record.renamed_pairs or 0) - (record.renamed_listed or 0)
        self._listing_caption.setText(
            f"{format_count(omitted)} correspondência(s) de conteúdos com muitas cópias não "
            "foram listadas individualmente, mas estão contadas nos indicadores."
            if omitted > 0 and kind in (None, "identical_renamed")
            else ""
        )
        table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            a_text = row.a.rel_path if row.a else "—"
            b_text = row.b.rel_path if row.b else "—"
            size = row.size_bytes
            if row.kind == "different" and row.a and row.b and row.a.size_bytes != row.b.size_bytes:
                size_text = f"{format_bytes(row.a.size_bytes)} / {format_bytes(row.b.size_bytes)}"
            else:
                size_text = format_bytes(size)
            table.setItem(index, 0, _item(KIND_LABELS.get(row.kind, row.kind)))
            table.setItem(index, 1, _item(a_text, tooltip=row.a.full_path if row.a else None))
            table.setItem(index, 2, _item(b_text, tooltip=row.b.full_path if row.b else None))
            table.setItem(index, 3, _item(size_text, right=True))
            note = describe(row)
            table.setItem(index, 4, _item(note, tooltip=note))

    def _on_result_selected(self) -> None:
        index = self._results_table.currentRow()
        if not 0 <= index < len(self._rows):
            self._clear_details()
            return
        row = self._rows[index]
        values = {
            "kind": KIND_LABELS.get(row.kind, row.kind),
            "note": describe(row),
            "a_path": row.a.full_path if row.a else "—",
            "a_meta": (
                f"{format_bytes(row.a.size_bytes)}  ·  modificado em {format_epoch(row.a.modified_at)}"
                if row.a
                else "—"
            ),
            "a_sha": (row.a.sha256 or "não calculado (não necessário ou indisponível)")
            if row.a
            else "—",
            "b_path": row.b.full_path if row.b else "—",
            "b_meta": (
                f"{format_bytes(row.b.size_bytes)}  ·  modificado em {format_epoch(row.b.modified_at)}"
                if row.b
                else "—"
            ),
            "b_sha": (row.b.sha256 or "não calculado (não necessário ou indisponível)")
            if row.b
            else "—",
        }
        for key, text in values.items():
            self._detail_values[key].setText(text)

    def _clear_details(self) -> None:
        for value in getattr(self, "_detail_values", {}).values():
            value.setText("—")

    def _update_controls(self) -> None:
        if not hasattr(self, "_start_button"):
            return
        running = self.is_running()
        _a, _b, warning, allowed = self._selection_state()
        self._selection_warning.setText(warning)
        self._selector_a.set_controls_enabled(not running)
        self._selector_b.set_controls_enabled(not running)
        self._start_button.setEnabled(not running and allowed and self._context.database_ok)
        self._cancel_button.setEnabled(running and not self._cancelling)
