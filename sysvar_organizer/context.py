"""Montagem dos serviços compartilhados pela aplicação."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sysvar_organizer.config import paths
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.persistence.comparison_repository import ComparisonRepository
from sysvar_organizer.persistence.consolidation_store import ConsolidationStore
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
    database_error_category: str | None = None
    startup_notes: tuple[str, ...] = ()
    consolidations: ConsolidationStore | None = None

    @property
    def database_ok(self) -> bool:
        return self.database is not None


INSPECTION_MESSAGES = {
    "missing": "O banco de dados ainda não foi criado (será criado na primeira abertura normal).",
    "outdated": "O banco de dados usa o esquema v{version}; a atualização para a versão atual é "
    "aplicada somente na abertura normal do aplicativo.",
    "newer": "O banco de dados foi criado por uma versão mais recente do aplicativo "
    "(esquema v{version}).",
    "corrupted": "O arquivo do banco de dados local está danificado ou não é um banco SQLite válido.",
    "inaccessible": "Não foi possível acessar o arquivo do banco de dados local.",
}


def build_context(recover_interrupted: bool = True, read_only: bool = False) -> AppContext:
    """Inicializa o banco local. Em caso de falha, o aplicativo segue sem persistência.

    ``recover_interrupted=False`` não altera registros de atividades em
    andamento (usado quando outra instância do aplicativo está aberta).
    ``read_only=True`` (diagnóstico) não cria, não migra e não altera o banco:
    apenas o abre se já estiver na versão atual. O arquivo do banco nunca é
    apagado ou recriado aqui.
    """
    db_path = paths.database_path()
    database = Database(db_path)
    if read_only:
        state, found = database.inspect()
        if state != "current":
            logger.info("Diagnóstico sem abrir o banco (estado=%s)", state)
            return AppContext(
                data_dir=paths.user_data_dir(),
                database_path=db_path,
                log_path=paths.log_path(),
                database=None,
                schema_version=found,
                settings=None,
                history=None,
                database_error=INSPECTION_MESSAGES[state].format(version=found),
                database_error_category=state,
            )
    try:
        version = database.inspect()[1] if read_only else database.initialize()
    except DatabaseError as exc:
        logger.error("Banco de dados indisponível (categoria=%s)", exc.category, exc_info=True)
        return AppContext(
            data_dir=paths.user_data_dir(),
            database_path=db_path,
            log_path=paths.log_path(),
            database=None,
            schema_version=None,
            settings=None,
            history=None,
            database_error=str(exc),
            database_error_category=exc.category,
        )

    logger.info("Banco de dados pronto (esquema v%d)", version)
    history = AnalysisHistoryRepository(database)
    duplicates = DuplicateRepository(database)
    comparisons = ComparisonRepository(database)
    visuals = VisualRepository(database)
    organizations = OrganizationRepository(database)
    operations = OperationsStore(database)
    consolidations = ConsolidationStore(database)

    notes: list[str] = []
    if recover_interrupted and not read_only:
        for repository, text in (
            (history, "análise(s) de armazenamento interrompida(s) registrada(s) como falha"),
            (duplicates, "busca(s) de duplicados interrompida(s) registrada(s) como falha"),
            (comparisons, "comparação(ões) de pastas interrompida(s) registrada(s) como falha"),
            (visuals, "busca(s) de fotografias interrompida(s) registrada(s) como falha"),
            (organizations, "simulação(ões) de organização interrompida(s) registrada(s) como falha"),
            (operations, "ação(ões) de operação interrompida(s); reconciliação obrigatória"),
            (consolidations, "arquivo(s) da consolidação interrompido(s); retomada disponível"),
        ):
            try:
                count = repository.mark_interrupted()
            except DatabaseError as exc:
                logger.error(
                    "Falha ao ajustar estados interrompidos (%s, categoria=%s)",
                    type(repository).__name__,
                    exc.category,
                )
                continue
            if count:
                logger.warning("%d %s", count, text)
                notes.append(f"{count} {text}")
    elif not read_only:
        logger.warning("Outra instância aberta: estados em andamento não foram alterados")

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
        startup_notes=tuple(notes),
        consolidations=consolidations,
    )
