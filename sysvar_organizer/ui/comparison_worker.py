"""Execução da comparação de pastas em thread separada da interface."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from sysvar_organizer.engines.comparison.folders import FolderComparer
from sysvar_organizer.engines.comparison.models import ComparisonOutcome, ComparisonSet
from sysvar_organizer.engines.file_analysis.models import STATUS_FAILED
from sysvar_organizer.persistence.comparison_store import ComparisonStore
from sysvar_organizer.persistence.database import Database

logger = logging.getLogger(__name__)


class ComparisonWorker(QObject):
    """Executa uma comparação. Deve ser movido para uma QThread antes de ``run``."""

    progress = Signal(object)  # ComparisonProgress
    finished = Signal(object)  # ComparisonOutcome

    def __init__(self, database: Database, set_a: ComparisonSet, set_b: ComparisonSet) -> None:
        super().__init__()
        self._database = database
        self._set_a = set_a
        self._set_b = set_b
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Solicita o cancelamento. Seguro para chamada a partir da interface."""
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        store: ComparisonStore | None = None
        try:
            store = ComparisonStore(self._database)
            comparer = FolderComparer(store, self._cancel, self.progress.emit)
            result = comparer.run(self._set_a, self._set_b)
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira resultado 'failed'
            logger.exception("Não foi possível executar a comparação de pastas")
            result = ComparisonOutcome(
                comparison_id=store.comparison_id if store is not None else None,
                status=STATUS_FAILED,
                files_a=0,
                files_b=0,
                processed=0,
                read_count=0,
                reused=0,
                bytes_read=0,
                elapsed_seconds=0.0,
                error_message=(
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Não foi possível executar a comparação. Detalhes no log do aplicativo."
                ),
            )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Falha ao fechar a conexão da comparação")
        self.finished.emit(result)
        QThread.currentThread().quit()
