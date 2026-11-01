"""Página Analisar Armazenamento: seleção de locais, execução e resultados."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.file_analysis.locations import (
    is_volume_root,
    normalize_location,
    overlapping_location,
    protected_location_warning,
)
from sysvar_organizer.engines.file_analysis.models import (
    KIND_DRIVE,
    KIND_FOLDER,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    ScanLocation,
    ScanProgress,
    ScanResult,
)
from sysvar_organizer.persistence.repositories import AnalysisRecord
from sysvar_organizer.ui.drive_dialog import DriveSelectionDialog
from sysvar_organizer.ui.formatting import (
    format_bytes,
    format_count,
    format_duration,
    format_timestamp,
    status_label,
)
from sysvar_organizer.ui.scan_worker import ScanWorker
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


class StoragePage(QWidget):
    analysis_finished = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._locations: list[ScanLocation] = []
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None
        self._cancelling = False
        self._busy_check: Callable[[], bool] | None = None
        self._elapsed = QElapsedTimer()
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick)

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Analisar Armazenamento",
                "Indexação de pastas e unidades escolhidas por você. A análise apenas "
                "lê nomes, tamanhos e datas: nenhum arquivo é alterado, movido ou excluído.",
                badge("Somente leitura", "ok"),
            )
        )
        layout.addWidget(self._build_selection())
        layout.addWidget(self._build_execution())
        layout.addWidget(self._build_results())
        layout.addStretch(1)

        self._show_last_completed()
        self._update_controls()

    # Construção ----------------------------------------------------------

    def _build_selection(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Seleção", "SectionTitle"))
        box.addWidget(label("Locais selecionados para a análise", "FieldLabel"))

        self._list = QListWidget()
        self._list.setObjectName("LocationList")
        self._list.setMinimumHeight(110)
        self._list.setMaximumHeight(170)
        self._list.itemSelectionChanged.connect(self._update_controls)
        box.addWidget(self._list)

        self._empty_hint = label(
            "Nenhum local selecionado. Use os botões abaixo para incluir pastas ou unidades.",
            "MetricHint",
            wrap=True,
        )
        box.addWidget(self._empty_hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._add_folder_button = QPushButton("Selecionar pasta")
        self._add_drive_button = QPushButton("Selecionar unidade")
        self._remove_button = QPushButton("Remover seleção")
        self._add_folder_button.clicked.connect(self._select_folder)
        self._add_drive_button.clicked.connect(self._select_drives)
        self._remove_button.clicked.connect(self._remove_selected)
        row.addWidget(self._add_folder_button)
        row.addWidget(self._add_drive_button)
        row.addWidget(self._remove_button)
        row.addStretch(1)
        box.addLayout(row)
        return frame

    def _build_execution(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Execução", "SectionTitle"))

        row = QHBoxLayout()
        row.setSpacing(8)
        self._start_button = QPushButton("Iniciar análise")
        self._start_button.setObjectName("PrimaryButton")
        self._cancel_button = QPushButton("Cancelar análise")
        self._start_button.clicked.connect(self._start)
        self._cancel_button.clicked.connect(self._cancel)
        row.addWidget(self._start_button)
        row.addWidget(self._cancel_button)
        row.addStretch(1)
        box.addLayout(row)

        self._progress = QProgressBar()
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFixedHeight(6)
        box.addSpacing(4)
        box.addWidget(self._progress)

        self._state_label = label("Aguardando início.", "BodyText")
        box.addWidget(self._state_label)
        self._current_label = ElidedLabel("", "MetricHint")
        box.addWidget(self._current_label)

        if not self._context.database_ok:
            box.addWidget(
                label(
                    "O banco de dados local está indisponível; a análise não pode ser iniciada.",
                    "BodyText",
                    wrap=True,
                )
            )
        return frame

    def _build_results(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Resultados", "SectionTitle"))
        heading.addStretch(1)
        box.addLayout(heading)
        self._results_caption = label("", "MetricHint", wrap=True)
        box.addWidget(self._results_caption)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self._files_tile = MetricCard("ARQUIVOS", "Tile", "TileValue")
        self._folders_tile = MetricCard("PASTAS", "Tile", "TileValue")
        self._bytes_tile = MetricCard("VOLUME EXAMINADO", "Tile", "TileValue")
        self._time_tile = MetricCard("TEMPO DECORRIDO", "Tile", "TileValue")
        self._issues_tile = MetricCard("IGNORADOS OU INACESSÍVEIS", "Tile", "TileValue")
        self._status_tile = MetricCard("ESTADO", "Tile", "TileValue")
        tiles = [
            self._files_tile,
            self._folders_tile,
            self._bytes_tile,
            self._time_tile,
            self._issues_tile,
            self._status_tile,
        ]
        for index, tile in enumerate(tiles):
            grid.addWidget(tile, index // 3, index % 3)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        box.addSpacing(4)
        box.addLayout(grid)
        return frame

    # Seleção -------------------------------------------------------------

    def _select_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Selecionar pasta para análise")
        if path:
            normalized = normalize_location(path)
            kind = KIND_DRIVE if is_volume_root(normalized) else KIND_FOLDER
            self._add_location(normalized, kind)

    def _select_drives(self) -> None:
        dialog = DriveSelectionDialog(self)
        if dialog.exec():
            for path in dialog.selected_paths():
                self._add_location(normalize_location(path), KIND_DRIVE)

    def _add_location(self, path: str, kind: str) -> None:
        overlap = overlapping_location(path, [loc.path for loc in self._locations])
        if overlap is not None:
            QMessageBox.information(
                self,
                APP_NAME,
                f"O local\n{path}\nsobrepõe um local já selecionado:\n{overlap}\n\n"
                "Para não contar os mesmos arquivos duas vezes, ele não foi incluído.",
            )
            return
        warning = protected_location_warning(path)
        if warning is not None:
            answer = QMessageBox.warning(
                self,
                "Pasta protegida do Windows",
                f"{path}\n\n{warning}\n\nDeseja incluir este local?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._locations.append(ScanLocation(path, kind))
        kind_text = "Unidade" if kind == KIND_DRIVE else "Pasta"
        self._list.addItem(f"{path}      ({kind_text})")
        self._update_controls()

    def _remove_selected(self) -> None:
        row = self._list.currentRow()
        if row < 0 or self.is_running():
            return
        self._list.takeItem(row)
        del self._locations[row]
        self._update_controls()

    # Execução ------------------------------------------------------------

    def is_running(self) -> bool:
        return self._thread is not None

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        """Função que indica outra tarefa em andamento (ex.: busca de duplicados)."""
        self._busy_check = check

    def _start(self) -> None:
        database = self._context.database
        if self.is_running() or not self._locations or database is None:
            return
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self,
                APP_NAME,
                "Há outra tarefa em andamento. Aguarde a conclusão para iniciar uma "
                "nova análise.",
            )
            return

        self._cancelling = False
        self._worker = ScanWorker(database, self._locations)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._on_thread_finished)

        self._results_caption.setText("Análise em andamento.")
        self._show_counts(0, 0, 0, 0, 0)
        self._time_tile.set_value("0 s")
        self._status_tile.set_value(status_label("running"))
        self._state_label.setText("Análise em andamento…")
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
            self._state_label.setText("Cancelando… aguardando a conclusão da etapa atual.")
            self._update_controls()

    def _on_progress(self, progress: ScanProgress) -> None:
        self._show_counts(
            progress.files,
            progress.folders,
            progress.total_bytes,
            progress.inaccessible,
            progress.skipped,
        )
        self._current_label.set_full_text(f"Examinando: {progress.current_path}")

    def _on_finished(self, result: ScanResult) -> None:
        self._clock.stop()
        self._progress.setRange(0, 1)
        self._progress.setValue(1 if result.status == STATUS_COMPLETED else 0)
        self._current_label.set_full_text("")

        self._show_counts(
            result.files, result.folders, result.total_bytes, result.inaccessible, result.skipped
        )
        self._time_tile.set_value(format_duration(result.elapsed_seconds))
        state = status_label(result.status, result.has_warnings)
        self._status_tile.set_value(state, self._status_hint(result.status, result.has_warnings))
        self._results_caption.setText("Resultado da análise executada agora.")

        if result.status == STATUS_COMPLETED:
            self._state_label.setText(f"Análise {state.lower()}.")
        elif result.status == STATUS_CANCELLED:
            self._state_label.setText(
                "Análise cancelada. Os itens examinados até o cancelamento foram registrados."
            )
        else:
            self._state_label.setText(result.error_message or "A análise falhou.")
        self.analysis_finished.emit()

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
            "Uma análise está em andamento. Deseja cancelá-la e sair?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if self._worker is not None:
            self._worker.cancel()
        if self._thread is not None and not self._thread.wait(15000):
            logger.warning("A análise não terminou no tempo de espera do encerramento")
        return True

    # Exibição ------------------------------------------------------------

    def _show_counts(
        self, files: int, folders: int, total_bytes: int, inaccessible: int, skipped: int
    ) -> None:
        self._files_tile.set_value(format_count(files))
        self._folders_tile.set_value(format_count(folders))
        self._bytes_tile.set_value(format_bytes(total_bytes), f"{format_count(total_bytes)} bytes")
        self._issues_tile.set_value(
            format_count(inaccessible + skipped),
            f"{format_count(inaccessible)} inacessíveis · {format_count(skipped)} ignorados "
            "(links, junções e pastas de sistema não seguidos)",
        )

    @staticmethod
    def _status_hint(status: str, has_warnings: bool) -> str:
        if status == STATUS_COMPLETED and has_warnings:
            return "Há itens inacessíveis ou ignorados."
        return ""

    def _show_last_completed(self) -> None:
        record: AnalysisRecord | None = None
        if self._context.history is not None:
            try:
                record = self._context.history.latest_completed()
            except DatabaseError:
                logger.exception("Falha ao consultar a última análise concluída")

        if record is None:
            self._results_caption.setText("Nenhuma análise concluída até o momento.")
            for tile in (
                self._files_tile,
                self._folders_tile,
                self._bytes_tile,
                self._time_tile,
                self._issues_tile,
                self._status_tile,
            ):
                tile.set_value("—")
            return

        self._results_caption.setText(
            f"Última análise concluída: {format_timestamp(record.started_at)}  ·  "
            f"{record.target_path}"
        )
        self._show_counts(
            record.files_count or 0,
            record.folders_count or 0,
            record.total_bytes or 0,
            record.inaccessible_count or 0,
            record.skipped_count or 0,
        )
        self._time_tile.set_value(format_duration(record.elapsed_seconds))
        self._status_tile.set_value(
            status_label(record.status, record.has_warnings),
            self._status_hint(record.status, record.has_warnings),
        )

    def _update_controls(self) -> None:
        running = self.is_running()
        has_locations = bool(self._locations)
        self._empty_hint.setVisible(not has_locations)
        self._add_folder_button.setEnabled(not running)
        self._add_drive_button.setEnabled(not running)
        self._remove_button.setEnabled(not running and self._list.currentRow() >= 0)
        self._start_button.setEnabled(
            not running and has_locations and self._context.database_ok
        )
        self._cancel_button.setEnabled(running and not self._cancelling)
