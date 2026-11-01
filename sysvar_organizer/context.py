"""Montagem dos serviços compartilhados pela aplicação."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sysvar_organizer.config import paths
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.persistence.database import Database
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

    return AppContext(
        data_dir=paths.user_data_dir(),
        database_path=db_path,
        log_path=paths.log_path(),
        database=database,
        schema_version=version,
        settings=SettingsRepository(database),
        history=history,
    )
