"""Execução da varredura em thread separada da interface."""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

from PySide6.QtCore import QObject, QThread, Signal, Slot

from sysvar_organizer.engines.file_analysis.models import (
    STATUS_FAILED,
    ScanLocation,
    ScanResult,
)
from sysvar_organizer.engines.file_analysis.scanner import StorageScanner
from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.scan_store import ScanStore

logger = logging.getLogger(__name__)


class ScanWorker(QObject):
    """Executa uma varredura. Deve ser movido para uma QThread antes de ``run``."""

    progress = Signal(object)  # ScanProgress
    finished = Signal(object)  # ScanResult

    def __init__(self, database: Database, locations: Sequence[ScanLocation]) -> None:
        super().__init__()
        self._database = database
        self._locations = list(locations)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Solicita o cancelamento. Seguro para chamada a partir da interface."""
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        store: ScanStore | None = None
        try:
            store = ScanStore(self._database)
            scanner = StorageScanner(store, self._cancel, self.progress.emit)
            result = scanner.run(self._locations)
        except Exception:  # noqa: BLE001 - qualquer falha vira resultado 'failed'
            logger.exception("Não foi possível executar a análise")
            result = ScanResult(
                analysis_id=store.analysis_id if store is not None else None,
                status=STATUS_FAILED,
                files=0,
                folders=0,
                total_bytes=0,
                inaccessible=0,
                skipped=0,
                elapsed_seconds=0.0,
                has_warnings=False,
                error_message="Não foi possível executar a análise. Detalhes no log do aplicativo.",
            )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Falha ao fechar a conexão da análise")
        self.finished.emit(result)
        # Encerra o laço da thread aqui mesmo, para que QThread.wait() funcione
        # mesmo quando a interface está bloqueada aguardando o término.
        QThread.currentThread().quit()
