"""Execução da busca de duplicados em thread separada da interface."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from sysvar_organizer.engines.comparison.duplicates import DuplicateFinder
from sysvar_organizer.engines.comparison.models import DuplicateResult
from sysvar_organizer.engines.file_analysis.models import STATUS_FAILED
from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.duplicate_store import DuplicateStore

logger = logging.getLogger(__name__)


class DuplicateWorker(QObject):
    """Executa uma busca. Deve ser movido para uma QThread antes de ``run``."""

    progress = Signal(object)  # DuplicateProgress
    finished = Signal(object)  # DuplicateResult

    def __init__(self, database: Database, analysis_id: int) -> None:
        super().__init__()
        self._database = database
        self._analysis_id = analysis_id
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Solicita o cancelamento. Seguro para chamada a partir da interface."""
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        store: DuplicateStore | None = None
        try:
            store = DuplicateStore(self._database)
            finder = DuplicateFinder(store, self._cancel, self.progress.emit)
            result = finder.run(self._analysis_id)
        except Exception:  # noqa: BLE001 - qualquer falha vira resultado 'failed'
            logger.exception("Não foi possível executar a busca de duplicados")
            result = DuplicateResult(
                search_id=store.search_id if store is not None else None,
                status=STATUS_FAILED,
                candidates=0,
                processed=0,
                read_count=0,
                reused=0,
                skipped=0,
                bytes_read=0,
                groups=None,
                group_files=None,
                logical_bytes=None,
                excess_copies=None,
                recoverable_bytes=None,
                hardlink_aliases=None,
                elapsed_seconds=0.0,
                error_message="Não foi possível executar a busca. Detalhes no log do aplicativo.",
            )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Falha ao fechar a conexão da busca")
        self.finished.emit(result)
        QThread.currentThread().quit()
