"""Acesso ao banco SQLite local e controle de versão do esquema.

A versão do esquema é mantida em ``PRAGMA user_version``. Cada migração é
aplicada em uma transação própria, junto com a atualização da versão.

O arquivo do banco nunca é apagado, recriado ou substituído automaticamente:
banco inacessível, danificado ou de versão mais recente é apenas relatado, e o
aplicativo segue sem persistência, preservando o arquivo como está.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sysvar_organizer.core.errors import (
    DatabaseCorrupted,
    DatabaseError,
    DatabaseInaccessible,
    SchemaIncompatible,
)

logger = logging.getLogger(__name__)

MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (
        1,
        (
            """
            CREATE TABLE settings (
                key        TEXT PRIMARY KEY NOT NULL,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE analysis_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_type TEXT NOT NULL,
                target_path   TEXT NOT NULL,
                status        TEXT NOT NULL
                              CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                files_count   INTEGER,
                total_bytes   INTEGER,
                error_message TEXT
            )
            """,
            "CREATE INDEX idx_analysis_history_started_at ON analysis_history (started_at)",
        ),
    ),
    (
        # Bloco 02 - varredura e indexação. Somente acréscimos: as tabelas e
        # os registros da versão 1 são preservados.
        2,
        (
            "ALTER TABLE analysis_history ADD COLUMN folders_count INTEGER",
            "ALTER TABLE analysis_history ADD COLUMN inaccessible_count INTEGER",
            "ALTER TABLE analysis_history ADD COLUMN skipped_count INTEGER",
            "ALTER TABLE analysis_history ADD COLUMN has_warnings INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE analysis_history ADD COLUMN elapsed_seconds REAL",
            """
            CREATE TABLE analysis_locations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id   INTEGER NOT NULL REFERENCES analysis_history (id),
                path          TEXT NOT NULL,
                kind          TEXT NOT NULL CHECK (kind IN ('folder', 'drive')),
                status        TEXT NOT NULL
                              CHECK (status IN ('pending', 'completed', 'cancelled',
                                                'inaccessible', 'failed')),
                files_count   INTEGER NOT NULL DEFAULT 0,
                folders_count INTEGER NOT NULL DEFAULT 0,
                total_bytes   INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_analysis_locations_analysis ON analysis_locations (analysis_id)",
            """
            CREATE TABLE indexed_folders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL REFERENCES analysis_history (id),
                location_id INTEGER NOT NULL REFERENCES analysis_locations (id),
                path        TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_indexed_folders_analysis ON indexed_folders (analysis_id)",
            """
            CREATE TABLE indexed_files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL REFERENCES analysis_history (id),
                folder_id   INTEGER NOT NULL REFERENCES indexed_folders (id),
                name        TEXT NOT NULL,
                extension   TEXT NOT NULL,
                size_bytes  INTEGER NOT NULL,
                created_at  INTEGER,
                modified_at INTEGER,
                accessed_at INTEGER,
                attributes  INTEGER
            )
            """,
            "CREATE INDEX idx_indexed_files_analysis ON indexed_files (analysis_id)",
            "CREATE INDEX idx_indexed_files_folder ON indexed_files (folder_id)",
            """
            CREATE TABLE analysis_issues (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id INTEGER NOT NULL REFERENCES analysis_history (id),
                kind        TEXT NOT NULL,
                path        TEXT NOT NULL,
                message     TEXT,
                occurred_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_analysis_issues_analysis ON analysis_issues (analysis_id)",
        ),
    ),
    (
        # Bloco 03 - duplicados exatos (SHA-256). Somente acréscimos.
        # Identificadores de volume e de arquivo do sistema podem exceder o
        # inteiro de 64 bits do SQLite e por isso são gravados como texto.
        3,
        (
            "CREATE INDEX idx_indexed_files_analysis_size ON indexed_files (analysis_id, size_bytes)",
            """
            CREATE TABLE duplicate_searches (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id       INTEGER NOT NULL REFERENCES analysis_history (id),
                status            TEXT NOT NULL
                                  CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
                started_at        TEXT NOT NULL,
                finished_at       TEXT,
                candidates_count  INTEGER NOT NULL DEFAULT 0,
                processed_count   INTEGER NOT NULL DEFAULT 0,
                read_count        INTEGER NOT NULL DEFAULT 0,
                reused_count      INTEGER NOT NULL DEFAULT 0,
                bytes_read        INTEGER NOT NULL DEFAULT 0,
                skipped_count     INTEGER NOT NULL DEFAULT 0,
                groups_count      INTEGER,
                group_files_count INTEGER,
                logical_bytes     INTEGER,
                excess_copies     INTEGER,
                recoverable_bytes INTEGER,
                hardlink_aliases  INTEGER,
                elapsed_seconds   REAL,
                error_message     TEXT
            )
            """,
            "CREATE INDEX idx_duplicate_searches_analysis ON duplicate_searches (analysis_id)",
            """
            CREATE TABLE duplicate_groups (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id         INTEGER NOT NULL REFERENCES duplicate_searches (id),
                sha256            TEXT NOT NULL,
                size_bytes        INTEGER NOT NULL,
                files_count       INTEGER NOT NULL,
                physical_count    INTEGER NOT NULL,
                excess_copies     INTEGER NOT NULL,
                recoverable_bytes INTEGER NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX idx_duplicate_groups_key
            ON duplicate_groups (search_id, size_bytes, sha256)
            """,
            """
            CREATE INDEX idx_duplicate_groups_ranking
            ON duplicate_groups (search_id, recoverable_bytes)
            """,
            """
            CREATE TABLE duplicate_files (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id         INTEGER NOT NULL REFERENCES duplicate_searches (id),
                file_id           INTEGER NOT NULL REFERENCES indexed_files (id),
                size_bytes        INTEGER NOT NULL,
                status            TEXT NOT NULL
                                  CHECK (status IN ('pending', 'hashed', 'not_needed', 'skipped')),
                sha256            TEXT,
                hash_reused       INTEGER NOT NULL DEFAULT 0,
                physical_key      TEXT,
                group_id          INTEGER REFERENCES duplicate_groups (id),
                is_hardlink_alias INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX idx_duplicate_files_search_size
            ON duplicate_files (search_id, size_bytes, sha256)
            """,
            "CREATE INDEX idx_duplicate_files_group ON duplicate_files (group_id)",
            """
            CREATE TABLE duplicate_issues (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id   INTEGER NOT NULL REFERENCES duplicate_searches (id),
                file_id     INTEGER REFERENCES indexed_files (id),
                kind        TEXT NOT NULL,
                path        TEXT NOT NULL,
                message     TEXT,
                occurred_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_duplicate_issues_search ON duplicate_issues (search_id)",
            """
            CREATE TABLE file_hash_cache (
                path_key    TEXT PRIMARY KEY NOT NULL,
                size_bytes  INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                volume_id   TEXT NOT NULL,
                file_index  TEXT NOT NULL,
                sha256      TEXT NOT NULL,
                computed_at TEXT NOT NULL
            )
            """,
        ),
    ),
    (
        # Bloco 04 - comparação de pastas. Somente acréscimos.
        4,
        (
            """
            CREATE TABLE folder_comparisons (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_a_id        INTEGER NOT NULL REFERENCES analysis_history (id),
                root_a               TEXT NOT NULL,
                analysis_b_id        INTEGER NOT NULL REFERENCES analysis_history (id),
                root_b               TEXT NOT NULL,
                status               TEXT NOT NULL
                                     CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
                started_at           TEXT NOT NULL,
                finished_at          TEXT,
                files_a              INTEGER NOT NULL DEFAULT 0,
                files_b              INTEGER NOT NULL DEFAULT 0,
                processed_count      INTEGER NOT NULL DEFAULT 0,
                read_count           INTEGER NOT NULL DEFAULT 0,
                reused_count         INTEGER NOT NULL DEFAULT 0,
                bytes_read           INTEGER NOT NULL DEFAULT 0,
                identical_pairs      INTEGER,
                identical_files_a    INTEGER,
                identical_files_b    INTEGER,
                renamed_pairs        INTEGER,
                renamed_listed       INTEGER,
                renamed_files_a      INTEGER,
                renamed_files_b      INTEGER,
                moved_pairs          INTEGER,
                different_count      INTEGER,
                only_a_count         INTEGER,
                only_b_count         INTEGER,
                indeterminate_count  INTEGER,
                same_physical_pairs  INTEGER,
                matched_files_a      INTEGER,
                matched_files_b      INTEGER,
                matched_physical     INTEGER,
                shared_contents      INTEGER,
                shared_bytes         INTEGER,
                folders_both         INTEGER,
                folders_only_a       INTEGER,
                folders_only_b       INTEGER,
                content_equivalent   INTEGER,
                structure_equivalent INTEGER,
                elapsed_seconds      REAL,
                error_message        TEXT
            )
            """,
            """
            CREATE TABLE comparison_files (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                comparison_id INTEGER NOT NULL REFERENCES folder_comparisons (id),
                side          TEXT NOT NULL CHECK (side IN ('A', 'B')),
                file_id       INTEGER NOT NULL REFERENCES indexed_files (id),
                rel_path      TEXT NOT NULL,
                rel_key       TEXT NOT NULL,
                folder_key    TEXT NOT NULL,
                name_key      TEXT NOT NULL,
                size_bytes    INTEGER NOT NULL,
                status        TEXT NOT NULL
                              CHECK (status IN ('pending', 'ok', 'indeterminate')),
                sha256        TEXT,
                hash_reused   INTEGER NOT NULL DEFAULT 0,
                physical_key  TEXT,
                issue_kind    TEXT,
                issue_message TEXT
            )
            """,
            """
            CREATE INDEX idx_comparison_files_rel
            ON comparison_files (comparison_id, side, rel_key)
            """,
            """
            CREATE INDEX idx_comparison_files_size
            ON comparison_files (comparison_id, size_bytes)
            """,
            """
            CREATE INDEX idx_comparison_files_hash
            ON comparison_files (comparison_id, sha256)
            """,
            """
            CREATE TABLE comparison_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                comparison_id INTEGER NOT NULL REFERENCES folder_comparisons (id),
                kind          TEXT NOT NULL
                              CHECK (kind IN ('identical', 'identical_renamed', 'different',
                                              'only_a', 'only_b', 'indeterminate')),
                a_entry_id    INTEGER REFERENCES comparison_files (id),
                b_entry_id    INTEGER REFERENCES comparison_files (id),
                size_bytes    INTEGER,
                same_physical INTEGER NOT NULL DEFAULT 0,
                name_differs  INTEGER NOT NULL DEFAULT 0,
                folder_differs INTEGER NOT NULL DEFAULT 0,
                detail        TEXT
            )
            """,
            """
            CREATE INDEX idx_comparison_results_kind
            ON comparison_results (comparison_id, kind)
            """,
            """
            CREATE TABLE comparison_folders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                comparison_id INTEGER NOT NULL REFERENCES folder_comparisons (id),
                rel_path      TEXT NOT NULL,
                status        TEXT NOT NULL CHECK (status IN ('both', 'only_a', 'only_b'))
            )
            """,
            """
            CREATE INDEX idx_comparison_folders_status
            ON comparison_folders (comparison_id, status)
            """,
        ),
    ),
    (
        # Bloco 05 - fotografias semelhantes (assinaturas perceptuais). Somente acréscimos.
        5,
        (
            """
            CREATE TABLE image_signatures (
                path_key    TEXT PRIMARY KEY NOT NULL,
                size_bytes  INTEGER NOT NULL,
                modified_ns INTEGER NOT NULL,
                volume_id   TEXT NOT NULL,
                file_index  TEXT NOT NULL,
                version     TEXT NOT NULL,
                phash       TEXT NOT NULL,
                dhash       TEXT NOT NULL,
                width       INTEGER NOT NULL,
                height      INTEGER NOT NULL,
                format      TEXT NOT NULL,
                computed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE visual_searches (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id      INTEGER NOT NULL REFERENCES analysis_history (id),
                root             TEXT NOT NULL,
                status           TEXT NOT NULL
                                 CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
                started_at       TEXT NOT NULL,
                finished_at      TEXT,
                criteria         TEXT NOT NULL,
                candidates_count INTEGER NOT NULL DEFAULT 0,
                processed_count  INTEGER NOT NULL DEFAULT 0,
                computed_count   INTEGER NOT NULL DEFAULT 0,
                reused_count     INTEGER NOT NULL DEFAULT 0,
                skipped_count    INTEGER NOT NULL DEFAULT 0,
                pairs_count      INTEGER,
                groups_count     INTEGER,
                grouped_images   INTEGER,
                exact_groups     INTEGER,
                very_high_groups INTEGER,
                probable_groups  INTEGER,
                uncertain_groups INTEGER,
                elapsed_seconds  REAL,
                error_message    TEXT
            )
            """,
            """
            CREATE TABLE visual_images (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id        INTEGER NOT NULL REFERENCES visual_searches (id),
                file_id          INTEGER NOT NULL REFERENCES indexed_files (id),
                status           TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'skipped')),
                phash            TEXT,
                dhash            TEXT,
                width            INTEGER,
                height           INTEGER,
                format           TEXT,
                signature_reused INTEGER NOT NULL DEFAULT 0,
                physical_key     TEXT,
                issue_kind       TEXT,
                issue_message    TEXT
            )
            """,
            "CREATE INDEX idx_visual_images_search ON visual_images (search_id, status)",
            """
            CREATE TABLE visual_groups (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id            INTEGER NOT NULL REFERENCES visual_searches (id),
                band                 TEXT NOT NULL
                                     CHECK (band IN ('exact', 'very_high', 'probable', 'uncertain')),
                members_count        INTEGER NOT NULL,
                max_phash_distance   INTEGER NOT NULL,
                max_dhash_distance   INTEGER NOT NULL,
                exact_members        INTEGER NOT NULL DEFAULT 0,
                representative_id    INTEGER REFERENCES visual_images (id)
            )
            """,
            "CREATE INDEX idx_visual_groups_search ON visual_groups (search_id, band)",
            """
            CREATE TABLE visual_group_members (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id       INTEGER NOT NULL REFERENCES visual_groups (id),
                search_id      INTEGER NOT NULL REFERENCES visual_searches (id),
                image_id       INTEGER NOT NULL REFERENCES visual_images (id),
                position       INTEGER NOT NULL,
                phash_distance INTEGER NOT NULL,
                dhash_distance INTEGER NOT NULL,
                sha256         TEXT,
                is_exact_copy  INTEGER NOT NULL DEFAULT 0,
                same_physical  INTEGER NOT NULL DEFAULT 0
            )
            """,
            "CREATE INDEX idx_visual_group_members_group ON visual_group_members (group_id)",
        ),
    ),
    (
        # Bloco 06 - classificação e prévia de organização (somente simulação).
        6,
        (
            """
            CREATE TABLE organization_simulations (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id        INTEGER NOT NULL REFERENCES analysis_history (id),
                root               TEXT NOT NULL,
                model              TEXT NOT NULL CHECK (model IN ('A', 'B', 'C')),
                destination        TEXT NOT NULL,
                exclude_sensitive  INTEGER NOT NULL,
                rule               TEXT NOT NULL,
                alerts             TEXT,
                status             TEXT NOT NULL
                                   CHECK (status IN ('running', 'completed', 'cancelled', 'failed')),
                started_at         TEXT NOT NULL,
                finished_at        TEXT,
                files_count        INTEGER NOT NULL DEFAULT 0,
                processed_count    INTEGER NOT NULL DEFAULT 0,
                total_bytes        INTEGER,
                proposal_bytes     INTEGER,
                ok_count           INTEGER,
                already_count      INTEGER,
                review_count       INTEGER,
                conflict_count     INTEGER,
                invalid_count      INTEGER,
                excluded_count     INTEGER,
                folders_count      INTEGER,
                structures_count   INTEGER,
                elapsed_seconds    REAL,
                error_message      TEXT
            )
            """,
            """
            CREATE TABLE organization_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                simulation_id   INTEGER NOT NULL REFERENCES organization_simulations (id),
                file_id         INTEGER NOT NULL REFERENCES indexed_files (id),
                category        TEXT NOT NULL,
                ambiguous       INTEGER NOT NULL DEFAULT 0,
                year            INTEGER,
                month           INTEGER,
                proposed_path   TEXT,
                proposed_key    TEXT,
                proposed_folder TEXT,
                status          TEXT NOT NULL
                                CHECK (status IN ('ok', 'already_in_place', 'review', 'conflict',
                                                  'invalid', 'excluded')),
                issues          TEXT NOT NULL DEFAULT '',
                rule            TEXT NOT NULL,
                justification   TEXT NOT NULL,
                structure_path  TEXT
            )
            """,
            """
            CREATE INDEX idx_organization_items_status
            ON organization_items (simulation_id, status)
            """,
            """
            CREATE INDEX idx_organization_items_category
            ON organization_items (simulation_id, category)
            """,
            """
            CREATE INDEX idx_organization_items_key
            ON organization_items (simulation_id, proposed_key)
            """,
            """
            CREATE INDEX idx_organization_items_folder
            ON organization_items (simulation_id, proposed_folder)
            """,
            """
            CREATE TABLE organization_structures (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                simulation_id INTEGER NOT NULL REFERENCES organization_simulations (id),
                path          TEXT NOT NULL,
                kind          TEXT NOT NULL,
                reason        TEXT NOT NULL,
                files_count   INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE INDEX idx_organization_structures_simulation
            ON organization_structures (simulation_id)
            """,
        ),
    ),
    (
        # Bloco 07 - planos de operação, revisão, execução (somente área de teste),
        # eventos e recuperação. Somente acréscimos.
        7,
        (
            """
            CREATE TABLE operation_plans (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_key      TEXT NOT NULL UNIQUE,
                simulation_id INTEGER REFERENCES organization_simulations (id),
                analysis_id   INTEGER REFERENCES analysis_history (id),
                environment   TEXT NOT NULL CHECK (environment IN ('real', 'test')),
                test_root     TEXT,
                operation     TEXT NOT NULL CHECK (operation IN ('move', 'copy')),
                destination   TEXT NOT NULL,
                status        TEXT NOT NULL CHECK (status IN ('open', 'cancelled')),
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                items_count   INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE operation_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id          INTEGER NOT NULL REFERENCES operation_plans (id),
                action_key       TEXT NOT NULL UNIQUE,
                unit_key         TEXT NOT NULL,
                file_id          INTEGER REFERENCES indexed_files (id),
                source_path      TEXT NOT NULL,
                destination_path TEXT,
                action_type      TEXT NOT NULL
                                 CHECK (action_type IN ('keep', 'copy', 'move',
                                                        'separate_review', 'restore')),
                justification    TEXT NOT NULL,
                size_bytes       INTEGER NOT NULL,
                expected_mtime   INTEGER,
                sensitive        INTEGER NOT NULL DEFAULT 0,
                alerts           TEXT NOT NULL DEFAULT '',
                dependencies     TEXT NOT NULL DEFAULT '',
                block_reason     TEXT,
                state            TEXT NOT NULL
                                 CHECK (state IN ('planned', 'validated', 'blocked', 'running',
                                                  'completed', 'failed', 'revert_pending',
                                                  'reverted', 'interrupted')),
                approved         INTEGER NOT NULL DEFAULT 0,
                reversible       INTEGER NOT NULL DEFAULT 0,
                src_volume       TEXT,
                src_index        TEXT,
                src_mtime_ns     INTEGER,
                sha256           TEXT,
                dst_volume       TEXT,
                dst_index        TEXT,
                dst_mtime_ns     INTEGER,
                updated_at       TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_operation_items_state ON operation_items (plan_id, state)",
            "CREATE INDEX idx_operation_items_unit ON operation_items (plan_id, unit_key)",
            """
            CREATE TABLE operation_reviews (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    INTEGER NOT NULL REFERENCES operation_items (id),
                decision   TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
                decided_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_operation_reviews_item ON operation_reviews (item_id)",
            """
            CREATE TABLE operation_executions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     INTEGER NOT NULL REFERENCES operation_items (id),
                kind        TEXT NOT NULL CHECK (kind IN ('execute', 'reconcile')),
                prior_state TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                result      TEXT,
                method      TEXT,
                message     TEXT
            )
            """,
            "CREATE INDEX idx_operation_executions_item ON operation_executions (item_id)",
            """
            CREATE TABLE operation_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id     INTEGER NOT NULL REFERENCES operation_plans (id),
                item_id     INTEGER REFERENCES operation_items (id),
                occurred_at TEXT NOT NULL,
                level       TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
                code        TEXT NOT NULL,
                message     TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_operation_events_plan ON operation_events (plan_id)",
            """
            CREATE TABLE operation_recoveries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id      INTEGER NOT NULL REFERENCES operation_items (id),
                requested_at TEXT NOT NULL,
                finished_at  TEXT,
                result       TEXT CHECK (result IN ('reverted', 'blocked', 'failed')),
                message      TEXT
            )
            """,
            "CREATE INDEX idx_operation_recoveries_item ON operation_recoveries (item_id)",
        ),
    ),
]

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


MESSAGE_CORRUPTED = (
    "O arquivo do banco de dados local está danificado ou não é um banco SQLite válido. "
    "O arquivo foi preservado sem alterações."
)
MESSAGE_INACCESSIBLE = (
    "Não foi possível acessar o arquivo do banco de dados local (permissão, bloqueio por "
    "outro programa ou local indisponível). O arquivo foi preservado sem alterações."
)
_CORRUPTION_MARKERS = ("not a database", "malformed", "corrupt", "file is encrypted")
_ACCESS_MARKERS = (
    "unable to open",
    "readonly",
    "read-only",
    "locked",
    "disk i/o",
    "permission",
    "access",
    "cannot open",
)
MAX_INTEGRITY_PROBLEMS = 20


def classify_error(exc: BaseException, message: str) -> DatabaseError:
    """Converte uma falha do SQLite/sistema na categoria de erro adequada."""
    text = str(exc).lower()
    if any(marker in text for marker in _CORRUPTION_MARKERS):
        return DatabaseCorrupted(MESSAGE_CORRUPTED)
    if isinstance(exc, OSError) or any(marker in text for marker in _ACCESS_MARKERS):
        return DatabaseInaccessible(MESSAGE_INACCESSIBLE)
    return DatabaseError(message)


@dataclass(frozen=True)
class IntegrityResult:
    """Resultado de uma verificação de integridade (somente leitura)."""

    status: str  # ok | problems | corrupted | inaccessible | missing
    full: bool
    problems: tuple[str, ...]
    foreign_key_issues: int
    schema_version: int | None
    elapsed_seconds: float

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def check_integrity(path: Path, full: bool = False) -> IntegrityResult:
    """Verifica a integridade do banco SEM alterá-lo (conexão somente leitura).

    ``full=False`` usa ``quick_check``; ``full=True`` usa ``integrity_check``.
    Também confere referências entre tabelas (``foreign_key_check``).
    """
    started = time.monotonic()

    def result(status: str, problems=(), fk: int = 0, version: int | None = None):
        return IntegrityResult(
            status, full, tuple(problems), fk, version, round(time.monotonic() - started, 3)
        )

    if not path.is_file():
        return result("missing", ("O arquivo do banco de dados não existe.",))
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except (OSError, sqlite3.Error):
        return result("inaccessible", (MESSAGE_INACCESSIBLE,))
    try:
        version = _user_version(conn)
        pragma = "integrity_check" if full else "quick_check"
        rows = conn.execute(f"PRAGMA {pragma}({MAX_INTEGRITY_PROBLEMS})").fetchall()
        problems = [str(r[0]) for r in rows if str(r[0]).lower() != "ok"]
        fk = 0
        for _row in conn.execute("PRAGMA foreign_key_check"):
            fk += 1
            if fk >= 1000:
                break
        if problems:
            return result("problems", problems, fk, version)
        if fk:
            return result(
                "problems",
                (f"{fk} registro(s) com referência inexistente entre tabelas.",),
                fk,
                version,
            )
        return result("ok", (), 0, version)
    except sqlite3.Error as exc:
        error = classify_error(exc, "Falha ao verificar o banco de dados local.")
        status = "corrupted" if isinstance(error, DatabaseCorrupted) else "inaccessible"
        return result(status, (str(error),))
    finally:
        conn.close()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> int:
        """Cria o banco, se necessário, e aplica migrações pendentes.

        Retorna a versão do esquema após a inicialização.
        """
        if self.path.exists() and not self.path.is_file():
            raise DatabaseInaccessible(MESSAGE_INACCESSIBLE)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise classify_error(exc, "Não foi possível abrir o banco de dados local.") from exc

        try:
            current = _user_version(conn)
            if current > LATEST_SCHEMA_VERSION:
                raise SchemaIncompatible(
                    "O banco de dados foi criado por uma versão mais recente do aplicativo "
                    f"(esquema v{current}; esta versão reconhece até a v{LATEST_SCHEMA_VERSION}). "
                    "O arquivo foi preservado sem alterações."
                )
            if current < LATEST_SCHEMA_VERSION:
                logger.info(
                    "Migração do esquema necessária: v%d -> v%d", current, LATEST_SCHEMA_VERSION
                )
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {int(version)}")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                logger.info("Esquema do banco atualizado para a versão %d", version)
                current = version
            return current
        except sqlite3.Error as exc:
            raise classify_error(exc, "Falha ao preparar o esquema do banco de dados.") from exc
        finally:
            conn.close()

    def check_integrity(self, full: bool = False) -> IntegrityResult:
        return check_integrity(self.path, full)

    def inspect(self) -> tuple[str, int | None]:
        """Estado do arquivo SEM criá-lo nem migrá-lo (conexão somente leitura).

        Retorna ``(estado, versão)``; estado: ``missing``, ``current``,
        ``outdated``, ``newer``, ``corrupted`` ou ``inaccessible``.
        """
        if not self.path.exists():
            return "missing", None
        if not self.path.is_file():
            return "inaccessible", None
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
        except (OSError, ValueError, sqlite3.Error):
            return "inaccessible", None
        try:
            version = _user_version(conn)
        except sqlite3.Error as exc:
            error = classify_error(exc, "")
            return ("corrupted" if isinstance(error, DatabaseCorrupted) else "inaccessible"), None
        finally:
            conn.close()
        if version > LATEST_SCHEMA_VERSION:
            return "newer", version
        if version < LATEST_SCHEMA_VERSION:
            return "outdated", version
        return "current", version

    def schema_version(self) -> int:
        with self.connection() as conn:
            return _user_version(conn)

    def open(self) -> sqlite3.Connection:
        """Abre uma conexão configurada. Quem abre é responsável por fechar.

        Usada por gravações longas (varredura), na própria thread que grava.
        Nunca cria o arquivo: se o banco tiver sido removido ou ficado
        indisponível depois da inicialização, a falha é relatada em vez de um
        banco vazio ser criado silenciosamente no lugar.
        """
        try:
            uri = self.path.resolve().as_uri() + "?mode=rw"
            conn = sqlite3.connect(uri, uri=True, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise classify_error(exc, "Não foi possível abrir o banco de dados local.") from exc
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Abre uma conexão, confirma ao final ou desfaz em caso de erro, e fecha."""
        conn = self.open()
        try:
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            _safe_rollback(conn)
            raise classify_error(exc, "Falha ao acessar o banco de dados local.") from exc
        except Exception:
            _safe_rollback(conn)
            raise
        finally:
            conn.close()


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """Desfaz a transação; uma falha aqui não pode encobrir o erro original."""
    try:
        conn.rollback()
    except sqlite3.Error:
        logger.warning("Falha ao desfazer transação após erro do banco")
