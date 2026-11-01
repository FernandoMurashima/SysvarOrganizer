"""Execução da busca de fotografias semelhantes em thread separada da interface."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from sysvar_organizer.engines.file_analysis.models import STATUS_FAILED
from sysvar_organizer.engines.visual_analysis.models import VisualOutcome
from sysvar_organizer.engines.visual_analysis.similar import SimilarityFinder
from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.visual_store import VisualStore

logger = logging.getLogger(__name__)


class VisualWorker(QObject):
    """Executa uma busca visual. Deve ser movido para uma QThread antes de ``run``."""

    progress = Signal(object)  # VisualProgress
    finished = Signal(object)  # VisualOutcome

    def __init__(self, database: Database, analysis_id: int, root: str) -> None:
        super().__init__()
        self._database = database
        self._analysis_id = analysis_id
        self._root = root
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Solicita o cancelamento. Seguro para chamada a partir da interface."""
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        store: VisualStore | None = None
        try:
            store = VisualStore(self._database)
            finder = SimilarityFinder(store, self._cancel, self.progress.emit)
            result = finder.run(self._analysis_id, self._root)
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira resultado 'failed'
            logger.exception("Não foi possível executar a busca de fotografias semelhantes")
            result = VisualOutcome(
                search_id=store.search_id if store is not None else None,
                status=STATUS_FAILED,
                candidates=0,
                processed=0,
                computed=0,
                reused=0,
                skipped=0,
                groups=None,
                grouped_images=None,
                elapsed_seconds=0.0,
                error_message=(
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Não foi possível executar a busca. Detalhes no log do aplicativo."
                ),
            )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Falha ao fechar a conexão da busca visual")
        self.finished.emit(result)
        QThread.currentThread().quit()
