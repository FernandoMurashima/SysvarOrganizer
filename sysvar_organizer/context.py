"""Montagem dos serviços compartilhados pela aplicação."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sysvar_organizer.config import paths
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.persistence.comparison_repository import ComparisonRepository
from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.duplicate_repository import DuplicateRepository
from sysvar_organizer.persistence.history_repository import HistoryRepository
from sysvar_organizer.persistence.operations_store import OperationsStore
from sysvar_organizer.persistence.organization_repository import OrganizationRepository
from sysvar_organizer.persistence.visual_repository import VisualRepository
from sysvar_organizer.persistence.repositories import (
    AnalysisHistoryRepository,
    SettingsRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    data_dir: Path
    database_path: Path
    log_path: Path
    database: Database | None
    schema_version: int | None
    settings: SettingsRepository | None
    history: AnalysisHistoryRepository | None
    database_error: str | None = None
    duplicates: DuplicateRepository | None = None
    comparisons: ComparisonRepository | None = None
    visuals: VisualRepository | None = None
    organizations: OrganizationRepository | None = None
    operations: OperationsStore | None = None
    history_log: HistoryRepository | None = None

    @property
    def database_ok(self) -> bool:
        return self.database is not None


def build_context() -> AppContext:
    """Inicializa o banco local. Em caso de falha, o aplicativo segue sem persistência."""
    db_path = paths.database_path()
    database = Database(db_path)
    try:
        version = database.initialize()
    except DatabaseError as exc:
        logger.exception("Banco de dados indisponível")
        return AppContext(
            data_dir=paths.user_data_dir(),
            database_path=db_path,
            log_path=paths.log_path(),
            database=None,
            schema_version=None,
            settings=None,
            history=None,
            database_error=str(exc),
        )

    logger.info("Banco de dados pronto (esquema v%d)", version)
    history = AnalysisHistoryRepository(database)
    try:
        interrupted = history.mark_interrupted()
        if interrupted:
            logger.warning("%d análise(s) interrompida(s) registrada(s) como falha", interrupted)
    except DatabaseError:
        logger.exception("Falha ao ajustar análises interrompidas")
    duplicates = DuplicateRepository(database)
    try:
        interrupted = duplicates.mark_interrupted()
        if interrupted:
            logger.warning(
                "%d busca(s) de duplicados interrompida(s) registrada(s) como falha", interrupted
            )
    except DatabaseError:
        logger.exception("Falha ao ajustar buscas de duplicados interrompidas")
    comparisons = ComparisonRepository(database)
    try:
        interrupted = comparisons.mark_interrupted()
        if interrupted:
            logger.warning(
                "%d comparação(ões) de pastas interrompida(s) registrada(s) como falha",
                interrupted,
            )
    except DatabaseError:
        logger.exception("Falha ao ajustar comparações interrompidas")
    visuals = VisualRepository(database)
    try:
        interrupted = visuals.mark_interrupted()
        if interrupted:
            logger.warning(
                "%d busca(s) de fotografias interrompida(s) registrada(s) como falha",
                interrupted,
            )
    except DatabaseError:
        logger.exception("Falha ao ajustar buscas de fotografias interrompidas")
    organizations = OrganizationRepository(database)
    try:
        interrupted = organizations.mark_interrupted()
        if interrupted:
            logger.warning(
                "%d simulação(ões) de organização interrompida(s) registrada(s) como falha",
                interrupted,
            )
    except DatabaseError:
        logger.exception("Falha ao ajustar simulações interrompidas")
    operations = OperationsStore(database)
    try:
        interrupted = operations.mark_interrupted()
        if interrupted:
            logger.warning(
                "%d ação(ões) de operação interrompida(s); reconciliação obrigatória", interrupted
            )
    except DatabaseError:
        logger.exception("Falha ao registrar operações interrompidas")

    return AppContext(
        data_dir=paths.user_data_dir(),
        database_path=db_path,
        log_path=paths.log_path(),
        database=database,
        schema_version=version,
        settings=SettingsRepository(database),
        history=history,
        duplicates=duplicates,
        comparisons=comparisons,
        visuals=visuals,
        organizations=organizations,
        operations=operations,
        history_log=HistoryRepository(database),
    )
