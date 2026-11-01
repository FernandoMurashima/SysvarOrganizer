"""Página Fotografias Semelhantes: busca por assinatura perceptual e revisão visual.

Somente consulta e seleção visual: não há controles de exclusão, movimentação
ou limpeza.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QElapsedTimer, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.engines.file_analysis.models import STATUS_CANCELLED, STATUS_COMPLETED
from sysvar_organizer.engines.visual_analysis.grouping import MAX_ASPECT_DIFFERENCE, THRESHOLDS
from sysvar_organizer.engines.visual_analysis.models import (
    PHASE_EXACT,
    PHASE_GROUPING,
    VisualOutcome,
    VisualProgress,
)
from sysvar_organizer.engines.visual_analysis.signatures import dependencies_error
from sysvar_organizer.engines.visual_analysis.similar import estimate_candidates
from sysvar_organizer.persistence.visual_repository import (
    VisualGroupRecord,
    VisualMemberRecord,
    VisualSearchRecord,
)
from sysvar_organizer.ui.formatting import (
    format_bytes,
    format_count,
    format_duration,
    format_epoch,
    format_timestamp,
    status_label,
)
from sysvar_organizer.ui.pages.compare import SetSelector
from sysvar_organizer.ui.thumbnails import THUMBNAIL_SIDE, ThumbnailLoader
from sysvar_organizer.ui.visual_worker import VisualWorker
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
MAX_THUMBNAILS = 24

BAND_LABELS = {
    "exact": "Cópia exata",
    "very_high": "Muito alta",
    "probable": "Provável",
    "uncertain": "Incerta — revisar",
}
BAND_FILTERS = [
    (None, "Todas as faixas"),
    ("exact", "Cópias exatas (SHA-256)"),
    ("very_high", "Similaridade muito alta"),
    ("probable", "Similaridade provável"),
    ("uncertain", "Similaridade incerta"),
]


def _explanation() -> str:
    vh, pr, un = (THRESHOLDS[b] for b in ("very_high", "probable", "uncertain"))
    return (
        "Distância perceptual: quantidade de bits diferentes entre as assinaturas de 64 bits "
        "de duas imagens (0 = visualmente iguais). São usadas duas assinaturas, pHash e dHash, "
        f"e as duas precisam atender ao limite. Muito alta: pHash ≤ {vh[0]} e dHash ≤ {vh[1]}. "
        f"Provável: ≤ {pr[0]} e ≤ {pr[1]}. Incerta: ≤ {un[0]} e ≤ {un[1]}, exige revisão. "
        f"Proporções diferentes (mais de {MAX_ASPECT_DIFFERENCE:.0%}) limitam a faixa a incerta. "
        "Cópia exata: conteúdo binário idêntico confirmado por SHA-256.\n"
        "A comparação perceptual pode gerar falsos positivos (imagens diferentes consideradas "
        "parecidas) e falsos negativos (versões da mesma foto não reconhecidas, por exemplo "
        "após recortes grandes). Confira as miniaturas antes de qualquer decisão."
    )


def _item(text: str, *, right: bool = False, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def _member_note(member: VisualMemberRecord) -> str:
    notes = []
    if member.position == 0:
        notes.append("Referência do grupo (maior resolução)")
    if member.same_physical:
        notes.append("Mesmo arquivo físico de outro integrante (hard link)")
    elif member.is_exact_copy:
        notes.append("Cópia exata (SHA-256) de outro integrante")
    return "; ".join(notes)


class ThumbnailTile(QFrame):
    def __init__(self, member: VisualMemberRecord) -> None:
        super().__init__()
        self.setObjectName("Tile")
        self.setFixedWidth(THUMBNAIL_SIDE + 34)
        box = QVBoxLayout(self)
        box.setContentsMargins(10, 10, 10, 10)
        box.setSpacing(4)
        self._image = QLabel("Carregando…")
        self._image.setObjectName("MetricHint")
        self._image.setFixedSize(THUMBNAIL_SIDE + 14, THUMBNAIL_SIDE + 14)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self._image)
        name = ElidedLabel(member.name, "FieldValue")
        name.setToolTip(member.full_path)
        box.addWidget(name)
        box.addWidget(
            label(
                f"{member.width} × {member.height}  ·  {format_bytes(member.size_bytes)}  ·  "
                f"{member.format}",
                "MetricHint",
                wrap=True,
            )
        )
        distance = (
            "Referência"
            if member.position == 0
            else f"Distância: pHash {member.phash_distance} · dHash {member.dhash_distance}"
        )
        box.addWidget(label(distance, "MetricHint", wrap=True))
        if member.is_exact_copy or member.same_physical:
            box.addWidget(
                label("Mesmo arquivo físico" if member.same_physical else "Cópia exata", "BodyText")
            )

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        if pixmap is None:
            self._image.setText("Miniatura indisponível")
        else:
            self._image.setPixmap(pixmap)


class PhotosPage(QWidget):
    search_finished = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._thread: QThread | None = None
        self._worker: VisualWorker | None = None
        self._cancelling = False
        self._busy_check: Callable[[], bool] | None = None
        self._search: VisualSearchRecord | None = None
        self._groups: list[VisualGroupRecord] = []
        self._tiles: dict[str, ThumbnailTile] = {}
        self._thumbnails = ThumbnailLoader(self)
        self._thumbnails.ready.connect(self._on_thumbnail)
        self._elapsed = QElapsedTimer()
        self._clock = QTimer(self)
        self._clock.setInterval(500)
        self._clock.timeout.connect(self._tick)
        self._missing_dependencies = dependencies_error()

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Fotografias Semelhantes",
                "Encontra fotografias visualmente parecidas mesmo com tamanhos, resoluções, "
                "nomes, formatos ou compressões diferentes. Somente consulta: nenhuma imagem é "
                "alterada, movida ou excluída.",
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
        box.addWidget(label("Seleção", "SectionTitle"))
        self._selector = SetSelector("Conjunto analisado", self._context)
        self._selector.changed.connect(self._on_selection_changed)
        box.addWidget(self._selector)
        self._estimate_label = label("", "BodyText", wrap=True)
        box.addWidget(self._estimate_label)
        if self._missing_dependencies:
            box.addWidget(badge("Dependência ausente", "error"))
            box.addWidget(label(self._missing_dependencies, "BodyText", wrap=True))
        row = QHBoxLayout()
        self._start_button = QPushButton("Buscar fotografias semelhantes")
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
        self._candidates_tile = MetricCard("IMAGENS CANDIDATAS", "Tile", "TileValue")
        self._examined_tile = MetricCard("EXAMINADAS", "Tile", "TileValue")
        self._signatures_tile = MetricCard("ASSINATURAS", "Tile", "TileValue")
        self._time_tile = MetricCard("TEMPO DECORRIDO", "Tile", "TileValue")
        for column, tile in enumerate(
            (self._candidates_tile, self._examined_tile, self._signatures_tile, self._time_tile)
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
        self._groups_tile = MetricCard("GRUPOS ENCONTRADOS", "Tile", "TileValue")
        self._images_tile = MetricCard("FOTOGRAFIAS ENVOLVIDAS", "Tile", "TileValue")
        self._exact_tile = MetricCard("CÓPIAS EXATAS", "Tile", "TileValue")
        self._skipped_tile = MetricCard("IGNORADAS", "Tile", "TileValue")
        self._very_high_tile = MetricCard("SIMILARIDADE MUITO ALTA", "Tile", "TileValue")
        self._probable_tile = MetricCard("SIMILARIDADE PROVÁVEL", "Tile", "TileValue")
        self._uncertain_tile = MetricCard("SIMILARIDADE INCERTA", "Tile", "TileValue")
        self._pairs_tile = MetricCard("PARES SEMELHANTES", "Tile", "TileValue")
        self._result_tiles = (
            self._groups_tile,
            self._images_tile,
            self._exact_tile,
            self._skipped_tile,
            self._very_high_tile,
            self._probable_tile,
            self._uncertain_tile,
            self._pairs_tile,
        )
        for index, tile in enumerate(self._result_tiles):
            grid.addWidget(tile, index // 4, index % 4)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        box.addSpacing(4)
        box.addLayout(grid)
        box.addSpacing(4)
        box.addWidget(label(_explanation(), "MetricHint", wrap=True))
        return frame

    def _build_groups(self) -> QWidget:
        frame, box = card()
        box.addWidget(label("Grupos de fotografias", "SectionTitle"))
        row = QHBoxLayout()
        self._band_filter = QComboBox()
        self._band_filter.setObjectName("AnalysisCombo")
        for value, text in BAND_FILTERS:
            self._band_filter.addItem(text, value)
        self._band_filter.currentIndexChanged.connect(lambda *_: self._load_groups(0))
        row.addWidget(self._band_filter)
        row.addStretch(1)
        box.addLayout(row)
        self._groups_table = result_table(
            ["Confiança", "Fotografias", "Distância máxima", "Referência", "Resolução"], (3,)
        )
        self._groups_table.setMinimumHeight(200)
        self._groups_table.itemSelectionChanged.connect(self._on_group_selected)
        box.addWidget(self._groups_table)
        self._groups_pager = Pager()
        self._groups_pager.page_changed.connect(self._load_groups)
        box.addWidget(self._groups_pager)

        box.addSpacing(8)
        box.addWidget(label("Comparação visual do grupo selecionado", "SectionTitle"))
        self._group_summary = label("Selecione um grupo para ver as miniaturas.", "MetricHint", wrap=True)
        box.addWidget(self._group_summary)
        self._strip = QWidget()
        self._strip_layout = QHBoxLayout(self._strip)
        self._strip_layout.setContentsMargins(0, 0, 0, 0)
        self._strip_layout.setSpacing(10)
        self._strip_layout.addStretch(1)
        strip_area = QScrollArea()
        strip_area.setWidgetResizable(True)
        strip_area.setFrameShape(QFrame.Shape.NoFrame)
        strip_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        strip_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        strip_area.setFixedHeight(THUMBNAIL_SIDE + 150)
        strip_area.setWidget(self._strip)
        box.addWidget(strip_area)

        self._members_table = result_table(
            ["Nome", "Caminho completo", "Resolução", "Tamanho", "Formato", "Distância", "Observação"],
            (1,),
        )
        self._members_table.setMinimumHeight(170)
        box.addWidget(self._members_table)
        return frame

    # Seleção -------------------------------------------------------------

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

    def _on_selection_changed(self) -> None:
        chosen = self._selector.current_set()
        repo = self._context.visuals
        if chosen is None or repo is None:
            self._estimate_label.setText("")
        else:
            try:
                count, size, _indexed = estimate_candidates(repo, chosen.analysis_id, chosen.root)
                self._estimate_label.setText(
                    f"Imagens candidatas estimadas: {format_count(count)} "
                    f"({format_bytes(size)}) — JPEG, PNG, WEBP, BMP e TIFF. "
                    "Vídeos e documentos não são abertos."
                )
            except DatabaseError:
                logger.exception("Falha ao estimar imagens candidatas")
                self._estimate_label.setText("")
        self._update_controls()

    # Execução ------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        self._busy_check = check

    def is_running(self) -> bool:
        return self._thread is not None

    def _start(self) -> None:
        database = self._context.database
        chosen = self._selector.current_set()
        if (
            self.is_running()
            or database is None
            or chosen is None
            or self._missing_dependencies
        ):
            return
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self, APP_NAME, "Há outra tarefa em andamento. Aguarde a conclusão para iniciar."
            )
            return
        self._cancelling = False
        self._worker = VisualWorker(database, chosen.analysis_id, chosen.root)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._on_thread_finished)

        self._show_search(None, "Busca em andamento.")
        for tile in (self._candidates_tile, self._examined_tile, self._signatures_tile):
            tile.set_value("—")
        self._time_tile.set_value("0 s")
        self._state_label.setText("Selecionando imagens candidatas…")
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
            self._state_label.setText("Cancelando… aguardando a imagem atual.")
            self._update_controls()

    def _on_progress(self, progress: VisualProgress) -> None:
        if progress.phase in (PHASE_GROUPING, PHASE_EXACT):
            self._progress.setRange(0, 0)
            if not self._cancelling:
                self._state_label.setText(
                    "Agrupando por semelhança…"
                    if progress.phase == PHASE_GROUPING
                    else "Confirmando cópias exatas por SHA-256…"
                )
        elif progress.candidates > 0:
            self._progress.setRange(0, progress.candidates)
            self._progress.setValue(progress.processed)
            if not self._cancelling:
                self._state_label.setText("Calculando assinaturas perceptuais…")
        self._candidates_tile.set_value(format_count(progress.candidates))
        self._examined_tile.set_value(
            format_count(progress.processed), f"{format_count(progress.skipped)} ignoradas"
        )
        self._signatures_tile.set_value(
            format_count(progress.computed + progress.reused),
            f"{format_count(progress.computed)} calculadas · "
            f"{format_count(progress.reused)} reaproveitadas",
        )
        self._current_label.set_full_text(
            f"Examinando: {progress.current_path}" if progress.current_path else ""
        )

    def _on_finished(self, outcome: VisualOutcome) -> None:
        self._clock.stop()
        self._time_tile.set_value(format_duration(outcome.elapsed_seconds))
        self._current_label.set_full_text("")
        self._progress.setRange(0, 1)
        self._progress.setValue(1 if outcome.status == STATUS_COMPLETED else 0)
        if outcome.status == STATUS_COMPLETED and outcome.search_id is not None:
            self._state_label.setText(
                f"Busca concluída: {format_count(outcome.groups)} grupo(s) de fotografias."
            )
            self._show_search(self._load_search(outcome.search_id), "Resultado da busca executada agora")
        else:
            if outcome.status == STATUS_CANCELLED:
                message = "Busca cancelada. Resultados parciais não são apresentados."
            else:
                message = outcome.error_message or "A busca falhou."
            self._state_label.setText(message)
            self._show_search(None, f"{status_label(outcome.status)}: nenhum resultado apresentado.")
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
        if self.is_running():
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "Uma busca de fotografias está em andamento. Deseja cancelá-la e sair?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
            if self._worker is not None:
                self._worker.cancel()
            if self._thread is not None and not self._thread.wait(15000):
                logger.warning("A busca visual não terminou no tempo de espera do encerramento")
        self._thumbnails.shutdown()
        return True

    # Resultados ----------------------------------------------------------

    def _load_search(self, search_id: int) -> VisualSearchRecord | None:
        repo = self._context.visuals
        if repo is None:
            return None
        try:
            return repo.search(search_id)
        except DatabaseError:
            logger.exception("Falha ao consultar a busca visual")
            return None

    def _show_latest_completed(self) -> None:
        repo = self._context.visuals
        record = None
        if repo is not None:
            try:
                record = repo.latest_completed()
            except DatabaseError:
                logger.exception("Falha ao consultar a última busca visual")
        if record is None:
            self._show_search(None, "Nenhuma busca de fotografias semelhantes concluída até o momento.")
        else:
            self._show_search(record, "Última busca concluída")

    def _show_search(self, record: VisualSearchRecord | None, title: str) -> None:
        self._search = record
        if record is None:
            self._results_caption.setText(title)
            for tile in self._result_tiles:
                tile.set_value("—")
            self._groups_table.setRowCount(0)
            self._groups_pager.set_total(0, PAGE_SIZE)
            self._clear_group()
            return
        r = record
        self._results_caption.setText(
            f"{title}: {format_timestamp(r.started_at)}  ·  {r.root}  ·  "
            f"{format_count(r.candidates_count)} imagens candidatas"
        )
        self._groups_tile.set_value(format_count(r.groups_count))
        self._images_tile.set_value(format_count(r.grouped_images), "Integrantes dos grupos")
        self._exact_tile.set_value(
            format_count(r.exact_groups), "Grupos só com cópias binariamente idênticas"
        )
        self._skipped_tile.set_value(
            format_count(r.skipped_count),
            "Corrompidas, incompatíveis, alteradas, removidas, inacessíveis ou só na nuvem",
        )
        self._very_high_tile.set_value(format_count(r.very_high_groups), "Grupos")
        self._probable_tile.set_value(format_count(r.probable_groups), "Grupos")
        self._uncertain_tile.set_value(format_count(r.uncertain_groups), "Grupos — revisão humana")
        self._pairs_tile.set_value(
            format_count(r.pairs_count), "Pares dentro das faixas, antes do agrupamento"
        )
        self._load_groups(0)

    def _load_groups(self, page: int) -> None:
        table = self._groups_table
        table.setRowCount(0)
        self._groups = []
        self._clear_group()
        record, repo = self._search, self._context.visuals
        if record is None or repo is None:
            return
        band = self._band_filter.currentData()
        try:
            total = repo.count_groups(record.id, band)
            self._groups_pager.set_total(total, PAGE_SIZE, page)
            self._groups = repo.groups(record.id, band, self._groups_pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar os grupos de fotografias")
            return
        table.setRowCount(len(self._groups))
        for index, group in enumerate(self._groups):
            table.setItem(index, 0, _item(BAND_LABELS.get(group.band, group.band)))
            table.setItem(index, 1, _item(format_count(group.members_count), right=True))
            table.setItem(
                index,
                2,
                _item(f"pHash {group.max_phash_distance} · dHash {group.max_dhash_distance}"),
            )
            table.setItem(index, 3, _item(group.representative_name, tooltip=group.representative_name))
            table.setItem(
                index, 4, _item(f"{group.representative_width} × {group.representative_height}")
            )
        if self._groups:
            table.selectRow(0)

    def _clear_group(self) -> None:
        self._thumbnails.reset()
        for tile in self._tiles.values():
            self._strip_layout.removeWidget(tile)
            tile.deleteLater()
        self._tiles = {}
        self._members_table.setRowCount(0)
        self._group_summary.setText("Selecione um grupo para ver as miniaturas.")

    def _on_group_selected(self) -> None:
        index = self._groups_table.currentRow()
        if not 0 <= index < len(self._groups):
            return
        group = self._groups[index]
        repo = self._context.visuals
        if repo is None:
            return
        try:
            members = repo.members(group.id)
        except DatabaseError:
            logger.exception("Falha ao consultar os integrantes do grupo")
            return
        self._clear_group()
        shown = members[:MAX_THUMBNAILS]
        extra = (
            f"  ·  exibindo {len(shown)} miniaturas de {len(members)}; todos os integrantes "
            "estão na tabela abaixo"
            if len(members) > len(shown)
            else ""
        )
        self._group_summary.setText(
            f"{BAND_LABELS.get(group.band, group.band)}  ·  {format_count(len(members))} "
            f"fotografias  ·  distância máxima pHash {group.max_phash_distance}, dHash "
            f"{group.max_dhash_distance}{extra}"
        )
        for position, member in enumerate(shown):
            key = f"{member.full_path}|{member.size_bytes}|{member.modified_at}"
            tile = ThumbnailTile(member)
            self._strip_layout.insertWidget(position, tile)
            self._tiles[key] = tile
            cached = self._thumbnails.request(key, member.full_path)
            if cached is not False:
                tile.set_pixmap(cached)

        table = self._members_table
        table.setRowCount(len(members))
        for row, member in enumerate(members):
            distance = (
                "Referência"
                if member.position == 0
                else f"pHash {member.phash_distance} · dHash {member.dhash_distance}"
            )
            table.setItem(row, 0, _item(member.name, tooltip=member.name))
            table.setItem(row, 1, _item(member.full_path, tooltip=member.full_path))
            table.setItem(row, 2, _item(f"{member.width} × {member.height}"))
            table.setItem(
                row,
                3,
                _item(
                    format_bytes(member.size_bytes),
                    right=True,
                    tooltip=f"Modificado em {format_epoch(member.modified_at)}"
                    + (f"\nSHA-256: {member.sha256}" if member.sha256 else ""),
                ),
            )
            table.setItem(row, 4, _item(member.format))
            table.setItem(row, 5, _item(distance))
            note = _member_note(member)
            table.setItem(row, 6, _item(note, tooltip=note))

    def _on_thumbnail(self, key: str, pixmap: QPixmap | None) -> None:
        tile = self._tiles.get(key)
        if tile is not None:
            tile.set_pixmap(pixmap)

    def _update_controls(self) -> None:
        if not hasattr(self, "_start_button"):
            return
        running = self.is_running()
        self._selector.set_controls_enabled(not running)
        self._start_button.setEnabled(
            not running
            and self._context.database_ok
            and self._selector.current_set() is not None
            and not self._missing_dependencies
        )
        self._cancel_button.setEnabled(running and not self._cancelling)
