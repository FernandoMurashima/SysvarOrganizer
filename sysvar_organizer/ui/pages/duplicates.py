"""Página Arquivos Duplicados: busca de duplicados exatos e consulta dos grupos.

Somente consulta e seleção visual: não há controles de exclusão, movimentação
ou limpeza.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.comparison.models import DuplicateProgress, DuplicateResult
from sysvar_organizer.engines.file_analysis.models import STATUS_CANCELLED, STATUS_COMPLETED
from sysvar_organizer.persistence.duplicate_repository import (
    DuplicateGroupRecord,
    DuplicateSearchRecord,
    SourceAnalysis,
)
from sysvar_organizer.ui.duplicate_worker import DuplicateWorker
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
    badge,
    card,
    label,
    page_header,
    page_layout,
)

logger = logging.getLogger(__name__)

GROUP_DISPLAY_LIMIT = 1000


def _item(text: str, *, right: bool = False, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def _table(headers: list[str], stretch_column: int) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setObjectName("ResultTable")
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    header = table.horizontalHeader()
    for column in range(len(headers)):
        mode = (
            QHeaderView.ResizeMode.Stretch
            if column == stretch_column
            else QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(column, mode)
    return table


class DuplicatesPage(QWidget):
    search_finished = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._analyses: list[SourceAnalysis] = []
        self._thread: QThread | None = None
        self._worker: DuplicateWorker | None = None
        self._cancelling = False
        self._busy_check: Callable[[], bool] | None = None
        self._elapsed = QElapsedTimer()
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick)

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Arquivos Duplicados",
                "Duplicados exatos: arquivos com conteúdo binário idêntico (SHA-256), "
                "independentemente de nome, extensão ou pasta. Somente consulta: nenhum "
                "arquivo é alterado, movido ou excluído.",
                badge("Somente leitura", "ok"),
            )
        )
        layout.addWidget(self._build_selection())
        layout.addWidget(self._build_processing())
        layout.addWidget(self._build_results())
        layout.addWidget(self._build_groups())
        layout.addStretch(1)

        self.reload_analyses()
        self._show_latest_completed()
        self._update_controls()

    # Construção ----------------------------------------------------------

    def _build_selection(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Seleção de análise", "SectionTitle"))
        box.addWidget(
            label(
                "A busca considera somente os arquivos de uma análise de armazenamento "
                "concluída. Análises de datas diferentes não são misturadas.",
                "MetricHint",
                wrap=True,
            )
        )
        self._combo = QComboBox()
        self._combo.setObjectName("AnalysisCombo")
        self._combo.currentIndexChanged.connect(self._on_analysis_changed)
        box.addWidget(self._combo)
        self._analysis_details = label("", "BodyText", wrap=True)
        box.addWidget(self._analysis_details)

        row = QHBoxLayout()
        self._start_button = QPushButton("Buscar duplicados")
        self._start_button.setObjectName("PrimaryButton")
        self._start_button.clicked.connect(self._start)
        row.addWidget(self._start_button)
        row.addStretch(1)
        box.addLayout(row)
        return frame

    def _build_processing(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Processamento", "SectionTitle"))
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
        self._candidates_tile = MetricCard("CANDIDATOS", "Tile", "TileValue")
        self._processed_tile = MetricCard("PROCESSADOS", "Tile", "TileValue")
        self._read_tile = MetricCard("DADOS LIDOS", "Tile", "TileValue")
        self._time_tile = MetricCard("TEMPO DECORRIDO", "Tile", "TileValue")
        for column, tile in enumerate(
            (self._candidates_tile, self._processed_tile, self._read_tile, self._time_tile)
        ):
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
        self._groups_tile = MetricCard("GRUPOS DE DUPLICADOS", "Tile", "TileValue")
        self._files_tile = MetricCard("ARQUIVOS NOS GRUPOS", "Tile", "TileValue")
        self._excess_tile = MetricCard("CÓPIAS EXCEDENTES", "Tile", "TileValue")
        self._recoverable_tile = MetricCard(
            "ESPAÇO POTENCIALMENTE RECUPERÁVEL", "Tile", "TileValue"
        )
        self._skipped_tile = MetricCard("IGNORADOS POR ERROS OU ALTERAÇÕES", "Tile", "TileValue")
        self._hardlinks_tile = MetricCard("HARD LINKS NÃO CONTABILIZADOS", "Tile", "TileValue")
        tiles = (
            self._groups_tile,
            self._files_tile,
            self._excess_tile,
            self._recoverable_tile,
            self._skipped_tile,
            self._hardlinks_tile,
        )
        for index, tile in enumerate(tiles):
            grid.addWidget(tile, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        box.addSpacing(4)
        box.addLayout(grid)
        return frame

    def _build_groups(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Grupos de duplicados exatos", "SectionTitle"))
        self._groups_caption = label("", "MetricHint", wrap=True)
        box.addWidget(self._groups_caption)

        self._groups_table = _table(
            ["Arquivos", "Tamanho de cada", "Recuperável", "Exemplo de nome", "SHA-256"], 3
        )
        self._groups_table.setMinimumHeight(220)
        self._groups_table.itemSelectionChanged.connect(self._on_group_selected)
        box.addWidget(self._groups_table)

        box.addSpacing(8)
        box.addWidget(label("Integrantes do grupo selecionado", "SectionTitle"))
        self._group_summary = label("Selecione um grupo para ver seus integrantes.", "MetricHint")
        box.addWidget(self._group_summary)
        self._group_hash = label("", "FieldValue")
        self._group_hash.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self._group_hash)

        self._members_table = _table(
            ["Nome", "Caminho completo", "Tamanho", "Modificado em", "Observação"], 1
        )
        self._members_table.setMinimumHeight(180)
        box.addWidget(self._members_table)
        return frame

    # Análises de origem ------------------------------------------------

    def reload_analyses(self) -> None:
        """Atualiza a lista de análises concluídas, mantendo a seleção atual."""
        if self.is_running():
            return
        current = self._combo.currentData()
        self._analyses = []
        repo = self._context.duplicates
        if repo is not None:
            try:
                self._analyses = repo.completed_analyses()
            except DatabaseError:
                logger.exception("Falha ao listar as análises concluídas")

        self._combo.blockSignals(True)
        self._combo.clear()
        for analysis in self._analyses:
            self._combo.addItem(
                f"{format_timestamp(analysis.started_at)}   ·   {analysis.target_path}   ·   "
                f"{format_count(analysis.files_count)} arquivos",
                analysis.id,
            )
        if current is not None:
            index = self._combo.findData(current)
            if index >= 0:
                self._combo.setCurrentIndex(index)
        self._combo.blockSignals(False)
        self._on_analysis_changed()

    def _selected_analysis(self) -> SourceAnalysis | None:
        analysis_id = self._combo.currentData()
        for analysis in self._analyses:
            if analysis.id == analysis_id:
                return analysis
        return None

    def _on_analysis_changed(self, *_args: object) -> None:
        analysis = self._selected_analysis()
        if analysis is None:
            self._analysis_details.setText(
                "Nenhuma análise de armazenamento concluída. Execute uma análise em "
                "Analisar Armazenamento antes de buscar duplicados."
            )
        else:
            self._analysis_details.setText(
                f"Data: {format_timestamp(analysis.started_at)}\n"
                f"Locais examinados: {';  '.join(analysis.locations) or analysis.target_path}\n"
                f"Arquivos indexados: {format_count(analysis.files_count)}  ·  "
                f"Volume: {format_bytes(analysis.total_bytes)}"
            )
        self._update_controls()

    # Execução ------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        """Função que indica outra tarefa em andamento (ex.: análise de armazenamento)."""
        self._busy_check = check

    def is_running(self) -> bool:
        return self._thread is not None

    def _start(self) -> None:
        database = self._context.database
        analysis = self._selected_analysis()
        if self.is_running() or database is None or analysis is None:
            return
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self,
                APP_NAME,
                "Há outra tarefa em andamento (análise de armazenamento ou comparação "
                "de pastas). Aguarde a conclusão para buscar duplicados.",
            )
            return

        self._cancelling = False
        self._worker = DuplicateWorker(database, analysis.id)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._on_thread_finished)

        self._clear_results("Busca em andamento.")
        for tile in (self._candidates_tile, self._processed_tile, self._read_tile):
            tile.set_value("—")
        self._time_tile.set_value("0 s")
        self._state_label.setText("Selecionando candidatos…")
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

    def _on_progress(self, progress: DuplicateProgress) -> None:
        if progress.candidates > 0:
            self._progress.setRange(0, progress.candidates)
            self._progress.setValue(progress.processed)
        if not self._cancelling:
            self._state_label.setText("Calculando SHA-256 dos candidatos…")
        self._candidates_tile.set_value(
            format_count(progress.candidates), "Arquivos com tamanho repetido"
        )
        self._processed_tile.set_value(
            format_count(progress.processed), f"de {format_count(progress.candidates)}"
        )
        self._read_tile.set_value(
            format_bytes(progress.bytes_read),
            f"{format_count(progress.read_count)} calculados · "
            f"{format_count(progress.reused)} reaproveitados",
        )
        self._current_label.set_full_text(
            f"Examinando: {progress.current_path}" if progress.current_path else ""
        )

    def _on_finished(self, result: DuplicateResult) -> None:
        self._clock.stop()
        self._time_tile.set_value(format_duration(result.elapsed_seconds))
        self._current_label.set_full_text("")
        self._progress.setRange(0, 1)
        self._progress.setValue(1 if result.status == STATUS_COMPLETED else 0)

        if result.status == STATUS_COMPLETED and result.search_id is not None:
            self._state_label.setText(
                f"Busca concluída: {format_count(result.groups)} grupo(s) de duplicados exatos."
            )
            record = self._load_search(result.search_id)
            if record is not None:
                self._show_search(record, "Resultado da busca executada agora")
        else:
            if result.status == STATUS_CANCELLED:
                message = "Busca cancelada. Resultados parciais não são apresentados."
            else:
                message = result.error_message or "A busca falhou."
            self._state_label.setText(message)
            self._clear_results(f"{status_label(result.status)}: nenhum resultado apresentado.")
            self._skipped_tile.set_value(format_count(result.skipped), "Até a interrupção")
        self.search_finished.emit()

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
            "Uma busca de duplicados está em andamento. Deseja cancelá-la e sair?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and not self._thread.wait(15000):
            logger.warning("A busca não terminou no tempo de espera do encerramento")
        return True

    # Resultados ----------------------------------------------------------

    def _load_search(self, search_id: int) -> DuplicateSearchRecord | None:
        repo = self._context.duplicates
        if repo is None:
            return None
        try:
            return repo.search(search_id)
        except DatabaseError:
            logger.exception("Falha ao consultar a busca de duplicados")
            return None

    def _show_latest_completed(self) -> None:
        repo = self._context.duplicates
        record = None
        if repo is not None:
            try:
                record = repo.latest_completed()
            except DatabaseError:
                logger.exception("Falha ao consultar a última busca de duplicados")
        if record is None:
            self._clear_results("Nenhuma busca de duplicados concluída até o momento.")
        else:
            self._show_search(record, "Última busca concluída")

    def _source_description(self, analysis_id: int) -> str:
        for analysis in self._analyses:
            if analysis.id == analysis_id:
                return f"análise de {format_timestamp(analysis.started_at)} ({analysis.target_path})"
        return f"análise nº {analysis_id}"

    def _show_search(self, record: DuplicateSearchRecord, title: str) -> None:
        self._results_caption.setText(
            f"{title}: {format_timestamp(record.started_at)}  ·  origem: "
            f"{self._source_description(record.analysis_id)}  ·  "
            f"{format_count(record.candidates_count)} candidatos"
        )
        self._groups_tile.set_value(format_count(record.groups_count), "Duplicados exatos")
        self._files_tile.set_value(
            format_count(record.group_files_count),
            f"Tamanho lógico: {format_bytes(record.logical_bytes)}",
        )
        self._excess_tile.set_value(
            format_count(record.excess_copies), "Cópias além de uma por grupo"
        )
        self._recoverable_tile.set_value(
            format_bytes(record.recoverable_bytes),
            "Estimativa lógica, mantendo uma cópia por grupo. Não é economia garantida "
            "de espaço físico em disco.",
        )
        self._skipped_tile.set_value(
            format_count(record.skipped_count),
            "Alterados, removidos, inacessíveis ou só na nuvem",
        )
        self._hardlinks_tile.set_value(
            format_count(record.hardlink_aliases),
            "Caminhos do mesmo arquivo físico; não somam espaço",
        )
        self._load_groups(record)

    def _clear_results(self, caption: str) -> None:
        self._results_caption.setText(caption)
        for tile in (
            self._groups_tile,
            self._files_tile,
            self._excess_tile,
            self._recoverable_tile,
            self._skipped_tile,
            self._hardlinks_tile,
        ):
            tile.set_value("—")
        self._groups_table.setRowCount(0)
        self._members_table.setRowCount(0)
        self._groups_caption.setText("")
        self._group_summary.setText("Selecione um grupo para ver seus integrantes.")
        self._group_hash.setText("")

    def _load_groups(self, record: DuplicateSearchRecord) -> None:
        groups: list[DuplicateGroupRecord] = []
        repo = self._context.duplicates
        if repo is not None:
            try:
                groups = repo.groups(record.id, GROUP_DISPLAY_LIMIT)
            except DatabaseError:
                logger.exception("Falha ao consultar os grupos de duplicados")

        total = record.groups_count or 0
        if total == 0:
            self._groups_caption.setText("Nenhum duplicado exato encontrado nesta busca.")
        elif total > len(groups):
            self._groups_caption.setText(
                f"Exibindo os {format_count(len(groups))} grupos com maior espaço "
                f"recuperável, de {format_count(total)}."
            )
        else:
            self._groups_caption.setText(
                f"{format_count(total)} grupo(s), ordenados pelo espaço potencialmente recuperável."
            )

        table = self._groups_table
        table.setRowCount(0)
        table.setRowCount(len(groups))
        for row, group in enumerate(groups):
            files_text = format_count(group.files_count)
            if group.physical_count != group.files_count:
                files_text += f" ({format_count(group.physical_count)} físicos)"
            first = _item(files_text, right=True)
            first.setData(Qt.ItemDataRole.UserRole, group.id)
            table.setItem(row, 0, first)
            table.setItem(row, 1, _item(format_bytes(group.size_bytes), right=True))
            table.setItem(row, 2, _item(format_bytes(group.recoverable_bytes), right=True))
            table.setItem(row, 3, _item(group.sample_name, tooltip=group.sample_name))
            table.setItem(row, 4, _item(group.sha256[:16] + "…", tooltip=group.sha256))
        self._members_table.setRowCount(0)
        self._group_summary.setText("Selecione um grupo para ver seus integrantes.")
        self._group_hash.setText("")
        if groups:
            table.selectRow(0)

    def _on_group_selected(self) -> None:
        row = self._groups_table.currentRow()
        item = self._groups_table.item(row, 0) if row >= 0 else None
        repo = self._context.duplicates
        if item is None or repo is None:
            return
        group_id = item.data(Qt.ItemDataRole.UserRole)
        try:
            members = repo.members(group_id)
        except DatabaseError:
            logger.exception("Falha ao consultar os integrantes do grupo")
            return

        if members:
            size = members[0].size_bytes
            physical = sum(1 for m in members if not m.is_hardlink_alias)
            recoverable = max(physical - 1, 0) * size
            self._group_summary.setText(
                f"{format_count(len(members))} arquivo(s) no grupo  ·  "
                f"{format_bytes(size)} cada  ·  potencialmente recuperável: "
                f"{format_bytes(recoverable)}"
            )
            self._group_hash.setText(f"SHA-256: {members[0].sha256}")

        table = self._members_table
        table.setRowCount(0)
        table.setRowCount(len(members))
        for index, member in enumerate(members):
            full_path = os.path.join(member.folder, member.name)
            notes = []
            if member.is_hardlink_alias:
                notes.append("Hard link: mesmo arquivo físico de outro integrante")
            if member.hash_reused:
                notes.append("SHA-256 reaproveitado (metadados revalidados)")
            table.setItem(index, 0, _item(member.name, tooltip=member.name))
            table.setItem(index, 1, _item(full_path, tooltip=full_path))
            table.setItem(index, 2, _item(format_bytes(member.size_bytes), right=True))
            table.setItem(index, 3, _item(format_epoch(member.modified_at)))
            table.setItem(index, 4, _item("; ".join(notes)))

    def _update_controls(self) -> None:
        running = self.is_running()
        self._combo.setEnabled(not running)
        self._start_button.setEnabled(
            not running
            and self._context.database_ok
            and self._selected_analysis() is not None
        )
        self._cancel_button.setEnabled(running and not self._cancelling)
