"""Gravação dos resultados de uma varredura, em lotes e transações.

Cada instância usa uma conexão própria, aberta na thread que executa a
varredura, e deve ser fechada com :meth:`close` ao final.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.repositories import utc_now

ANALYSIS_TYPE_STORAGE_SCAN = "storage_scan"


class ScanStore:
    BATCH_ROWS = 2000
    COMMIT_INTERVAL_SECONDS = 1.0
    MAX_RECORDED_ISSUES = 1000

    def __init__(self, database: Database) -> None:
        self._conn = database.open()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._pending_files: list[tuple] = []
        self._last_commit = time.monotonic()
        self._issues_recorded = 0
        self.analysis_id: int | None = None

    # Início --------------------------------------------------------------

    def begin(self, locations: Sequence[tuple[str, str]], summary: str) -> list[int]:
        """Registra a análise e seus locais. Retorna os ids dos locais, na ordem."""
        cursor = self._conn.execute(
            """
            INSERT INTO analysis_history (
                analysis_type, target_path, status, started_at,
                files_count, folders_count, total_bytes,
                inaccessible_count, skipped_count, has_warnings
            ) VALUES (?, ?, 'running', ?, 0, 0, 0, 0, 0, 0)
            """,
            (ANALYSIS_TYPE_STORAGE_SCAN, summary, utc_now()),
        )
        self.analysis_id = int(cursor.lastrowid)
        ids = []
        for path, kind in locations:
            cursor = self._conn.execute(
                """
                INSERT INTO analysis_locations (analysis_id, path, kind, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (self.analysis_id, path, kind),
            )
            ids.append(int(cursor.lastrowid))
        self._commit()
        return ids

    # Durante a varredura -----------------------------------------------

    def add_folder(self, location_id: int, path: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO indexed_folders (analysis_id, location_id, path) VALUES (?, ?, ?)",
            (self.analysis_id, location_id, path),
        )
        return int(cursor.lastrowid)

    def add_file(
        self,
        folder_id: int,
        name: str,
        extension: str,
        size_bytes: int,
        created_at: int | None,
        modified_at: int | None,
        accessed_at: int | None,
        attributes: int | None,
    ) -> None:
        self._pending_files.append(
            (
                self.analysis_id,
                folder_id,
                name,
                extension,
                size_bytes,
                created_at,
                modified_at,
                accessed_at,
                attributes,
            )
        )
        if len(self._pending_files) >= self.BATCH_ROWS:
            self.flush()

    def add_issue(self, kind: str, path: str, message: str | None) -> None:
        """Registro resumido: guarda no máximo MAX_RECORDED_ISSUES por análise."""
        if self._issues_recorded >= self.MAX_RECORDED_ISSUES:
            return
        self._conn.execute(
            """
            INSERT INTO analysis_issues (analysis_id, kind, path, message, occurred_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (self.analysis_id, kind, path, message, utc_now()),
        )
        self._issues_recorded += 1

    def maybe_flush(self) -> None:
        if time.monotonic() - self._last_commit >= self.COMMIT_INTERVAL_SECONDS:
            self.flush()

    def flush(self) -> None:
        if self._pending_files:
            self._conn.executemany(
                """
                INSERT INTO indexed_files (
                    analysis_id, folder_id, name, extension, size_bytes,
                    created_at, modified_at, accessed_at, attributes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._pending_files,
            )
            self._pending_files.clear()
        self._commit()

    def finish_location(
        self, location_id: int, status: str, files: int, folders: int, total_bytes: int
    ) -> None:
        self._conn.execute(
            """
            UPDATE analysis_locations
            SET status = ?, files_count = ?, folders_count = ?, total_bytes = ?
            WHERE id = ?
            """,
            (status, files, folders, total_bytes, location_id),
        )

    # Encerramento --------------------------------------------------------

    def discard_pending(self) -> None:
        """Descarta o lote ainda não confirmado (usado após uma falha)."""
        self._pending_files.clear()
        self._conn.rollback()

    def finish(
        self,
        status: str,
        *,
        files: int,
        folders: int,
        total_bytes: int,
        inaccessible: int,
        skipped: int,
        has_warnings: bool,
        elapsed_seconds: float,
        error_message: str | None,
    ) -> None:
        self.flush()
        self._conn.execute(
            "UPDATE analysis_locations SET status = ? WHERE analysis_id = ? AND status = 'pending'",
            (status, self.analysis_id),
        )
        self._conn.execute(
            """
            UPDATE analysis_history
            SET status = ?, finished_at = ?, files_count = ?, folders_count = ?,
                total_bytes = ?, inaccessible_count = ?, skipped_count = ?,
                has_warnings = ?, elapsed_seconds = ?, error_message = ?
            WHERE id = ?
            """,
            (
                status,
                utc_now(),
                files,
                folders,
                total_bytes,
                inaccessible,
                skipped,
                int(has_warnings),
                round(elapsed_seconds, 3),
                error_message,
                self.analysis_id,
            ),
        )
        self._commit()

    def close(self) -> None:
        self._conn.close()

    def _commit(self) -> None:
        self._conn.commit()
        self._last_commit = time.monotonic()
