"""Execução da simulação de organização em thread separada da interface."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from sysvar_organizer.engines.file_analysis.models import STATUS_FAILED
from sysvar_organizer.engines.organization.models import OrganizationOutcome, OrganizationRequest
from sysvar_organizer.engines.organization.planner import OrganizationPlanner
from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.organization_store import OrganizationStore

logger = logging.getLogger(__name__)


class OrganizationWorker(QObject):
    """Executa uma simulação. Deve ser movido para uma QThread antes de ``run``."""

    progress = Signal(object)  # OrganizationProgress
    finished = Signal(object)  # OrganizationOutcome

    def __init__(self, database: Database, request: OrganizationRequest) -> None:
        super().__init__()
        self._database = database
        self._request = request
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Solicita o cancelamento. Seguro para chamada a partir da interface."""
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        store: OrganizationStore | None = None
        try:
            store = OrganizationStore(self._database)
            planner = OrganizationPlanner(store, self._cancel, self.progress.emit)
            result = planner.run(self._request)
        except Exception as exc:  # noqa: BLE001 - qualquer falha vira resultado 'failed'
            logger.exception("Não foi possível executar a simulação de organização")
            result = OrganizationOutcome(
                simulation_id=store.simulation_id if store is not None else None,
                status=STATUS_FAILED,
                total=0,
                processed=0,
                elapsed_seconds=0.0,
                error_message=(
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Não foi possível executar a simulação. Detalhes no log do aplicativo."
                ),
            )
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:  # noqa: BLE001
                    logger.exception("Falha ao fechar a conexão da simulação")
        self.finished.emit(result)
        QThread.currentThread().quit()
