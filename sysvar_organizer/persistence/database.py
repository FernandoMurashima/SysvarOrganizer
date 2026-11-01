"""Acesso ao banco SQLite local e controle de versão do esquema.

A versão do esquema é mantida em ``PRAGMA user_version``. Cada migração é
aplicada em uma transação própria, junto com a atualização da versão.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sysvar_organizer.core.errors import DatabaseError

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
]

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> int:
        """Cria o banco, se necessário, e aplica migrações pendentes.

        Retorna a versão do esquema após a inicialização.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError("Não foi possível abrir o banco de dados local.") from exc

        try:
            current = _user_version(conn)
            if current > LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    "O banco de dados foi criado por uma versão mais recente do aplicativo."
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
            raise DatabaseError("Falha ao preparar o esquema do banco de dados.") from exc
        finally:
            conn.close()

    def schema_version(self) -> int:
        with self.connection() as conn:
            return _user_version(conn)

    def open(self) -> sqlite3.Connection:
        """Abre uma conexão configurada. Quem abre é responsável por fechar.

        Usada por gravações longas (varredura), na própria thread que grava.
        """
        try:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as exc:
            raise DatabaseError("Não foi possível abrir o banco de dados local.") from exc
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Abre uma conexão, confirma ao final ou desfaz em caso de erro, e fecha."""
        conn = self.open()
        try:
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise DatabaseError("Falha ao acessar o banco de dados local.") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
