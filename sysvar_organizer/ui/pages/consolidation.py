"""Página Consolidação de unidades.

Move para o destino principal SOMENTE os arquivos cujo conteúdo (SHA-256) não
existe nele, com prévia completa, autorização explícita das duas unidades,
confirmação final, execução em segundo plano (progresso e cancelamento
seguro) e relatório de conferência. O destino nunca tem arquivos alterados,
renomeados ou excluídos; duplicados restantes na origem não são apagados.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.file_analysis.locations import is_same_or_inside
from sysvar_organizer.operations.consolidation import (
    ConsolidationAnalyzer,
    ConsolidationExecutor,
    ConsolidationRefused,
    coverage,
    exclude_items,
    execution_token,
    item_for_path,
    restore_items,
)
from sysvar_organizer.persistence.consolidation_store import JobRecord
from sysvar_organizer.reports.catalog import JOB_STATUS_LABELS, build_report
from sysvar_organizer.reports.export import EXTENSIONS, FORMAT_CSV, FORMAT_JSON, ExportError, export_report
from sysvar_organizer.reports.model import count_rows, page_rows
from sysvar_organizer.reports.values import cell_text
from sysvar_organizer.ui.background import BackgroundTask
from sysvar_organizer.ui.formatting import format_bytes, format_count, format_timestamp
from sysvar_organizer.ui.pages.compare import SetSelector
from sysvar_organizer.ui.widgets import (
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
RULES = (
    "O destino é a unidade principal e é preservado: nenhum arquivo existente nele é alterado, "
    "renomeado ou excluído. Somente arquivos da origem cujo conteúdo (SHA-256) não existe no "
    "destino são movidos — uma única cópia quando houver repetidos na origem. Cada arquivo é "
    "copiado, conferido no destino e só então removido da origem. Duplicados restantes na origem "
    "não são apagados e nada é formatado. Arquivos desmarcados (excluídos da transferência) "
    "permanecem intocados na origem."
)
DATASETS = (
    ("todos", "Todos os arquivos da origem"),
    ("previstos", "Previstos para transferência"),
    ("excluidos", "Excluídos da transferência (desmarcados)"),
    ("transferidos", "Transferidos"),
    ("mantidos", "Duplicados mantidos na origem"),
    ("pendentes", "Pendentes (a transferir, bloqueados, falhas, não verificados)"),
    ("renomeados", "Conflitos de nome resolvidos"),
)


def _item(text: str, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if tooltip:
        item.setToolTip(tooltip)
    return item


class ConsolidationPage(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._busy_check: Callable[[], bool] | None = None
        self._task = BackgroundTask(self)
        self._jobs: list[JobRecord] = []
        self._report = None
        self._row_ids: list[int] = []

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Consolidação de unidades",
                "Leva para a unidade principal somente o que existe apenas na outra unidade, "
                "com prévia, autorização, conferência por SHA-256 e relatório.",
                badge("Operação real controlada", "unavailable"),
            )
        )
        notice, notice_box = card("ReservedArea")
        notice_box.addWidget(label(RULES, "BodyText", wrap=True))
        layout.addWidget(notice)
        layout.addWidget(self._build_selection())
        layout.addWidget(self._build_preview())
        layout.addWidget(self._build_execution())
        layout.addStretch(1)
        self.reload_analyses()
        self.reload_jobs()

    # Construção ---------------------------------------------------------

    def _build_selection(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Unidades", "SectionTitle"))
        box.addWidget(
            label(
                "Use análises de armazenamento concluídas das duas unidades (a prévia reaproveita "
                "o índice e os SHA-256 já calculados, conferindo a validade de cada um).",
                "MetricHint",
                wrap=True,
            )
        )
        row = QHBoxLayout()
        self._source = SetSelector("Origem — unidade a esvaziar", self._context)
        self._dest = SetSelector("Destino — unidade principal (preservada)", self._context)
        row.addWidget(self._source, 1)
        row.addWidget(self._dest, 1)
        box.addLayout(row)
        box.addWidget(label("Pasta base no destino (opcional)", "FieldLabel"))
        base = QHBoxLayout()
        self._base = QLineEdit()
        self._base.setObjectName("TextInput")
        self._base.setPlaceholderText("Vazio = raiz do destino (mesma estrutura de pastas da origem)")
        choose = QPushButton("Escolher…")
        choose.clicked.connect(self._choose_base)
        base.addWidget(self._base, 1)
        base.addWidget(choose)
        box.addLayout(base)
        buttons = QHBoxLayout()
        self._analyze_button = QPushButton("Gerar prévia da consolidação")
        self._analyze_button.setObjectName("PrimaryButton")
        self._analyze_button.clicked.connect(self._analyze)
        buttons.addWidget(self._analyze_button)
        buttons.addStretch(1)
        box.addLayout(buttons)
        return frame

    def _build_preview(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Prévia", "SectionTitle"))
        self._job_combo = QComboBox()
        self._job_combo.setObjectName("AnalysisCombo")
        self._job_combo.currentIndexChanged.connect(lambda *_: self._show_job())
        box.addWidget(self._job_combo)
        grid = QGridLayout()
        grid.setSpacing(12)
        self._tiles = {}
        for index, (key, title) in enumerate(
            (
                ("transfer", "EXCLUSIVOS A TRANSFERIR"),
                ("volume", "VOLUME A TRANSFERIR"),
                ("in_dest", "JÁ EXISTEM NO DESTINO"),
                ("dups", "DUPLICADOS NA ORIGEM"),
                ("conflicts", "CONFLITOS DE NOME"),
                ("pending", "PENDÊNCIAS"),
                ("done", "TRANSFERIDOS"),
                ("space", "ESPAÇO LIVRE NO DESTINO"),
            )
        ):
            tile = MetricCard(title, "Tile", "TileValue")
            grid.addWidget(tile, index // 4, index % 4)
            self._tiles[key] = tile
        box.addLayout(grid)
        self._status = label("", "BodyText", wrap=True)
        box.addWidget(self._status)
        self._coverage = label("", "SectionTitle", wrap=True)
        box.addWidget(self._coverage)
        self._dataset_combo = QComboBox()
        self._dataset_combo.setObjectName("AnalysisCombo")
        for key, text in DATASETS:
            self._dataset_combo.addItem(text, key)
        self._dataset_combo.currentIndexChanged.connect(lambda *_: self._load_rows(0))
        box.addWidget(self._dataset_combo)
        self._table = result_table(["—"], ())
        self._table.setMinimumHeight(240)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.itemSelectionChanged.connect(self._update_controls)
        box.addWidget(self._table)
        self._pager = Pager()
        self._pager.page_changed.connect(self._load_rows)
        box.addWidget(self._pager)
        box.addWidget(label("Seleção de arquivos", "FieldLabel"))
        box.addWidget(
            label(
                "Desmarque arquivos que NÃO devem ser transferidos: eles não serão copiados, movidos, "
                "renomeados nem excluídos, e continuam na origem. A escolha fica gravada nesta "
                "consolidação (e é mantida se a prévia das mesmas unidades for gerada de novo).",
                "MetricHint",
                wrap=True,
            )
        )
        selection = QHBoxLayout()
        self._exclude_button = QPushButton("Excluir selecionados da transferência")
        self._restore_button = QPushButton("Devolver selecionados à transferência")
        self._exclude_button.clicked.connect(self._exclude_selected)
        self._restore_button.clicked.connect(self._restore_selected)
        selection.addWidget(self._exclude_button)
        selection.addWidget(self._restore_button)
        selection.addStretch(1)
        box.addLayout(selection)
        by_path = QHBoxLayout()
        self._path_input = QLineEdit()
        self._path_input.setObjectName("TextInput")
        self._path_input.setPlaceholderText("Caminho completo na origem, ex.: H:\\Pasta\\arquivo.bak")
        self._exclude_path_button = QPushButton("Excluir este arquivo da transferência")
        self._exclude_path_button.clicked.connect(self._exclude_path)
        by_path.addWidget(self._path_input, 1)
        by_path.addWidget(self._exclude_path_button)
        box.addLayout(by_path)
        return frame

    def _build_execution(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Execução", "SectionTitle"))
        heading.addStretch(1)
        box.addLayout(heading)
        row = QHBoxLayout()
        self._authorize_button = QPushButton("Autorizar as duas unidades…")
        self._execute_button = QPushButton("Executar consolidação")
        self._cancel_button = QPushButton("Cancelar com segurança")
        self._retry_button = QPushButton("Reprocessar bloqueios e falhas")
        self._authorize_button.clicked.connect(self._authorize)
        self._execute_button.clicked.connect(self._execute)
        self._cancel_button.clicked.connect(self._task.cancel)
        self._retry_button.clicked.connect(self._retry)
        for button in (self._authorize_button, self._execute_button, self._cancel_button, self._retry_button):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        self._progress = label("", "MetricHint", wrap=True)
        box.addWidget(self._progress)
        export = QHBoxLayout()
        self._csv_button = QPushButton("Relatório de conferência (CSV do conjunto exibido)")
        self._json_button = QPushButton("Relatório de conferência (JSON completo)")
        self._csv_button.clicked.connect(lambda: self._export(FORMAT_CSV))
        self._json_button.clicked.connect(lambda: self._export(FORMAT_JSON))
        export.addWidget(self._csv_button)
        export.addWidget(self._json_button)
        export.addStretch(1)
        box.addLayout(export)
        return frame

    # Tarefas ------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        self._busy_check = check

    def is_running(self) -> bool:
        return self._task.is_running()

    def request_shutdown(self) -> bool:
        if not self.is_running():
            return True
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "A consolidação está em andamento. Cancelar com segurança e sair? O arquivo em "
            "cópia é descartado e a origem é preservada; a retomada fica disponível.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if not self._task.wait(30000):
            logger.warning("A consolidação não terminou no tempo de espera do encerramento")
        return True

    def _can_start(self) -> bool:
        if self.is_running() or self._context.consolidations is None:
            return False
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(self, APP_NAME, "Há outra tarefa em andamento. Aguarde a conclusão.")
            return False
        return True

    def _start(self, text: str, function, on_done=None) -> None:
        def task(cancel, progress):
            return function(cancel, lambda phase, done, total: progress(done, total))

        self._progress.setText(f"{text}…")
        self._task.start(task, lambda r, e: self._finished(r, e, on_done), self._on_progress, self._after_task)
        self._update_controls()

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setText(f"Processando {format_count(done)} de {format_count(total)}…")

    def _finished(self, result, error, on_done) -> None:
        if error is not None:
            message = getattr(error, "message", None) or (
                str(error) if isinstance(error, DatabaseError)
                else "A tarefa não pôde ser concluída. Detalhes no log do aplicativo."
            )
            if not isinstance(error, (ConsolidationRefused, DatabaseError)):
                logger.error("Falha na consolidação", exc_info=error)
            self._progress.setText(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        if on_done is not None:
            on_done(result)

    def _after_task(self) -> None:
        self.reload_jobs()
        self._update_controls()

    # Dados --------------------------------------------------------------

    def reload_analyses(self) -> None:
        if self.is_running():
            return
        analyses = []
        repo = self._context.duplicates
        if repo is not None:
            try:
                analyses = repo.completed_analyses()
            except DatabaseError:
                logger.exception("Falha ao listar análises concluídas")
        self._source.set_analyses(analyses)
        self._dest.set_analyses(analyses)
        if len(analyses) > 1:
            self._dest.select_analysis_index(1)

    def reload_jobs(self, select: int | None = None) -> None:
        store = self._context.consolidations
        current = select if select is not None else self._job_combo.currentData()
        self._jobs = []
        if store is not None:
            try:
                self._jobs = store.jobs()
            except DatabaseError:
                logger.exception("Falha ao listar consolidações")
        self._job_combo.blockSignals(True)
        self._job_combo.clear()
        for job in self._jobs:
            self._job_combo.addItem(
                f"Consolidação nº {job.id}  ·  {format_timestamp(job.created_at)}  ·  "
                f"{job.source_root} → {job.dest_root}  ·  {JOB_STATUS_LABELS.get(job.status, job.status)}",
                job.id,
            )
        if current is not None:
            found = self._job_combo.findData(current)
            if found >= 0:
                self._job_combo.setCurrentIndex(found)
        self._job_combo.blockSignals(False)
        self._show_job()

    def _job(self) -> JobRecord | None:
        job_id = self._job_combo.currentData()
        return next((j for j in self._jobs if j.id == job_id), None)

    def _show_job(self) -> None:
        job, store = self._job(), self._context.consolidations
        self._report = None
        if job is None or store is None:
            for tile in self._tiles.values():
                tile.set_value("—")
            self._status.setText("Nenhuma prévia gerada.")
            self._coverage.setText("")
            self._load_rows(0)
            self._update_controls()
            return
        try:
            summary = store.summary(job.id)
            cov = coverage(store, job.id)
            with store.database.connection() as conn:
                self._report = build_report(conn, "consolidation", job.id)
        except DatabaseError:
            logger.exception("Falha ao consultar a consolidação")
            return
        done = sum(v[0] for (c, s), v in summary.items() if c == "transfer" and s == "transferred")
        done_bytes = sum(v[1] for (c, s), v in summary.items() if c == "transfer" and s == "transferred")
        remaining = sum(v[0] for (c, s), v in summary.items() if c == "transfer" and s in ("planned", "interrupted"))
        excluded = summary.get(("transfer", "excluded"), (0, 0))
        self._tiles["transfer"].set_value(
            format_count(job.transfer_count),
            f"{format_count(remaining)} ainda a transferir  ·  {format_count(excluded[0])} excluídos por você",
        )
        self._tiles["volume"].set_value(format_bytes(job.transfer_bytes))
        self._tiles["in_dest"].set_value(format_count(job.in_dest_count), format_bytes(job.in_dest_bytes) + " — mantidos na origem")
        self._tiles["dups"].set_value(format_count(job.source_dup_count), "cópias repetidas mantidas na origem")
        self._tiles["conflicts"].set_value(format_count(job.conflict_count), "nome alternativo, sem sobrescrita")
        self._tiles["pending"].set_value(
            format_count(job.pending_count),
            f"{format_count(job.source_issues)} não verificados na análise da origem",
        )
        self._tiles["done"].set_value(format_count(done), format_bytes(done_bytes))
        try:
            import shutil

            free = shutil.disk_usage(job.dest_root).free
            self._tiles["space"].set_value(format_bytes(free), "necessário: " + format_bytes(job.transfer_bytes))
        except OSError:
            self._tiles["space"].set_value("—", "destino indisponível")
        authorized = (
            f"Unidades autorizadas em {format_timestamp(job.authorized_at)}."
            if job.authorized_at else "Unidades ainda NÃO autorizadas."
        )
        self._status.setText(
            f"{JOB_STATUS_LABELS.get(job.status, job.status)}  ·  {authorized}\n"
            f"Origem: {job.source_root}  →  destino: {job.dest_base}"
            + (f"\n{job.message}" if job.message else "")
        )
        self._coverage.setText(
            "Cobertura comprovada para os arquivos indexados da origem — confirme com nova "
            "análise das duas unidades antes de qualquer formatação."
            if cov.proven
            else "A ORIGEM NÃO PODE SER FORMATADA: " + "; ".join(cov.reasons or ("prévia não executada",)) + "."
        )
        self._load_rows(0)
        self._update_controls()

    def _load_rows(self, page: int) -> None:
        table, report, store = self._table, self._report, self._context.consolidations
        table.setRowCount(0)
        self._row_ids = []
        if report is None or store is None:
            self._pager.set_total(0, PAGE_SIZE)
            return
        dataset = report.dataset(self._dataset_combo.currentData() or "todos")
        try:
            with store.database.connection() as conn:
                total = count_rows(conn, dataset)
                self._pager.set_total(total, PAGE_SIZE, page)
                rows = page_rows(conn, dataset, self._pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar os itens da consolidação")
            return
        table.setColumnCount(len(dataset.columns))
        table.setHorizontalHeaderLabels([c.header for c in dataset.columns])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setRowCount(len(rows))
        self._row_ids = [int(row[0]) for row in rows]
        for index, row in enumerate(rows):
            for column, (value, spec) in enumerate(zip(row, dataset.columns)):
                text = cell_text(value, spec.kind)
                table.setItem(index, column, _item(text, text if len(text) > 40 else None))
        table.resizeColumnsToContents()

    # Ações ----------------------------------------------------------------

    def _choose_base(self) -> None:
        chosen = self._dest.current_set()
        if chosen is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Pasta base no destino", chosen.root)
        if folder:
            folder = os.path.normpath(folder)
            if not is_same_or_inside(folder, chosen.root):
                QMessageBox.information(self, APP_NAME, "A pasta base precisa ficar dentro do destino.")
                return
            self._base.setText(folder)

    def _analyze(self) -> None:
        source, dest, store = self._source.current_set(), self._dest.current_set(), self._context.consolidations
        if source is None or dest is None or store is None or not self._can_start():
            return
        base = str(self._base.text()).strip() or dest.root
        try:
            job_id = ConsolidationAnalyzer(store).create(source.analysis_id, source.root,
                                                         dest.analysis_id, dest.root, base)
        except ConsolidationRefused as exc:
            QMessageBox.warning(self, APP_NAME, exc.message)
            return
        self.reload_jobs(job_id)
        self._start(
            "Gerando a prévia (somente leitura dos discos)",
            lambda cancel, progress: ConsolidationAnalyzer(store, cancel, progress).run(job_id),
            lambda job: self._progress.setText(job.message or ""),
        )

    def _authorize(self) -> None:
        job, store = self._job(), self._context.consolidations
        if job is None or store is None or not self._can_start():
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Autorizar a consolidação entre estas DUAS unidades?\n\n"
            f"Origem (arquivos exclusivos serão MOVIDOS daqui): {job.source_root}\n"
            f"Destino principal (preservado, só recebe arquivos novos): {job.dest_root}\n"
            f"Pasta base no destino: {job.dest_base}\n\n"
            "A autorização vale somente para estas unidades físicas (identificação conferida "
            "antes de cada execução). Nada é executado agora.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            ConsolidationExecutor(store).authorize(job.id)
        except (ConsolidationRefused, DatabaseError) as exc:
            QMessageBox.warning(self, APP_NAME, getattr(exc, "message", str(exc)))
        self.reload_jobs(job.id)

    def _execute(self) -> None:
        job, store = self._job(), self._context.consolidations
        if job is None or store is None or not self._can_start():
            return
        remaining = store.count_items(job.id, "transfer", ("planned", "interrupted"))
        excluded = store.count_items(job.id, "transfer", "excluded")
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Confirmar a CONSOLIDAÇÃO REAL?\n\n"
            f"Operação: mover arquivos exclusivos ({format_count(remaining)} pendentes de "
            f"{format_count(job.transfer_count)}; {format_bytes(job.transfer_bytes)} na prévia)\n"
            f"Excluídos da transferência por você (não serão tocados): {format_count(excluded)}\n"
            f"Origem: {job.source_root}\n"
            f"Destino: {job.dest_base}\n\n"
            "Cada arquivo é copiado, conferido por SHA-256 no destino e só então removido da "
            "origem. Nada é sobrescrito; arquivos do destino não são alterados; duplicados da "
            "origem são mantidos. A execução pode ser cancelada com segurança e retomada.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        job_id, token = job.id, execution_token(job)

        def show(outcome):
            counts = outcome.counts
            QMessageBox.information(
                self,
                APP_NAME,
                f"{outcome.message}\n\nTransferidos: {counts.get('transferred', 0)}  ·  "
                f"já no destino: {counts.get('covered', 0)}  ·  bloqueados: {counts.get('blocked', 0)}  ·  "
                f"falhas: {counts.get('failed', 0)}  ·  retomados: {counts.get('reconciled', 0)}",
            )

        self._start(
            "Consolidando",
            lambda cancel, progress: ConsolidationExecutor(store, cancel, progress).execute(job_id, token),
            show,
        )

    def _selected_ids(self) -> list[int]:
        rows = sorted({item.row() for item in self._table.selectedItems()})
        return [self._row_ids[r] for r in rows if 0 <= r < len(self._row_ids)]

    def _apply_selection(self, action, item_ids: list[int], verb: str) -> None:
        job, store = self._job(), self._context.consolidations
        if job is None or store is None or self.is_running() or not item_ids:
            return
        try:
            result = action(store, job.id, item_ids)
        except (ConsolidationRefused, DatabaseError) as exc:
            QMessageBox.warning(self, APP_NAME, getattr(exc, "message", str(exc)))
            return
        lines = [f"{len(result.changed)} arquivo(s) {verb}."]
        for item_id, reason in result.refused[:10]:
            lines.append(f"Item nº {item_id}: {reason}")
        if len(result.refused) > 10:
            lines.append(f"… e mais {len(result.refused) - 10} item(ns) não alterado(s).")
        QMessageBox.information(self, APP_NAME, "\n".join(lines))
        self.reload_jobs(job.id)

    def _exclude_selected(self) -> None:
        self._apply_selection(exclude_items, self._selected_ids(), "excluído(s) da transferência (permanecem intocados na origem)")

    def _restore_selected(self) -> None:
        self._apply_selection(restore_items, self._selected_ids(), "devolvido(s) à transferência")

    def _exclude_path(self) -> None:
        job, store = self._job(), self._context.consolidations
        path = str(self._path_input.text()).strip()
        if job is None or store is None or self.is_running() or not path:
            return
        try:
            item = item_for_path(store, job.id, path)
        except DatabaseError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        if item is None:
            QMessageBox.information(
                self, APP_NAME,
                f"O arquivo não faz parte desta consolidação (fora da origem {job.source_root} ou "
                f"não indexado na análise):\n{path}",
            )
            return
        answer = QMessageBox.question(
            self, APP_NAME,
            f"Excluir da transferência?\n\n{item.source_path}\n({format_bytes(item.size_bytes)})\n\n"
            "O arquivo não será copiado, movido, renomeado nem excluído; continuará na origem.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._apply_selection(exclude_items, [item.id], "excluído(s) da transferência (permanece intocado na origem)")
        self._path_input.clear()

    def _retry(self) -> None:
        job, store = self._job(), self._context.consolidations
        if job is None or store is None or self.is_running():
            return
        changed = ConsolidationExecutor(store).retry_failed(job.id)
        QMessageBox.information(
            self, APP_NAME, f"{changed} arquivo(s) voltaram para a fila e serão revalidados na execução."
        )
        self.reload_jobs(job.id)

    def _export(self, fmt: str) -> None:
        job, store = self._job(), self._context.consolidations
        database = self._context.database
        if job is None or store is None or database is None or not self._can_start():
            return
        extension = EXTENSIONS[fmt]
        folder = Path.home() / "Documents"
        folder = folder if folder.is_dir() else Path.home()
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        dataset = self._dataset_combo.currentData() if fmt == FORMAT_CSV else None
        name = f"sysvar-organizer-consolidacao-{job.id}{'-' + dataset if dataset else ''}-{stamp}{extension}"
        path, _ = QFileDialog.getSaveFileName(
            self, "Relatório de conferência", str(folder / name), f"{fmt.upper()} (*{extension})",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != extension:
            destination = destination.with_name(destination.name + extension)
        overwrite = False
        if os.path.lexists(destination):
            answer = QMessageBox.question(
                self, APP_NAME, f"Substituir o arquivo existente?\n{destination}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        job_id = job.id

        def function(cancel, progress):
            conn = database.open()
            try:
                report = build_report(conn, "consolidation", job_id)
                return export_report(conn, report, destination, fmt, dataset_key=dataset,
                                     overwrite=overwrite, cancel=cancel,
                                     progress=lambda d, t: progress("Exportando", d, t))
            finally:
                conn.close()

        def done(result):
            QMessageBox.information(self, APP_NAME, f"Relatório gravado com {result.rows} registros.\n\n{result.path}")

        try:
            self._start("Exportando o relatório de conferência", function, done)
        except ExportError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))

    # Estado ---------------------------------------------------------------

    def _update_controls(self) -> None:
        job = self._job()
        running = self.is_running()
        ready = job is not None and job.analyzed_at is not None and job.status in (
            "planned", "partial", "interrupted", "cancelled", "completed")
        has_pending = job is not None and job.transfer_count > 0
        self._analyze_button.setEnabled(not running and self._context.consolidations is not None)
        self._authorize_button.setEnabled(ready and not running and job.authorized_at is None)
        self._execute_button.setEnabled(
            ready and not running and job.authorized_at is not None and has_pending
            and job.status != "completed"
        )
        self._cancel_button.setEnabled(running)
        self._retry_button.setEnabled(ready and not running)
        editable = ready and not running
        selected = editable and bool(self._table.selectedItems())
        self._exclude_button.setEnabled(selected)
        self._restore_button.setEnabled(selected)
        self._exclude_path_button.setEnabled(editable)
        self._csv_button.setEnabled(job is not None and not running)
        self._json_button.setEnabled(job is not None and not running)
