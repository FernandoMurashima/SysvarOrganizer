"""Consultas sobre análises de origem e resultados de buscas de duplicados."""

from __future__ import annotations

from dataclasses import dataclass

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.repositories import utc_now
from sysvar_organizer.persistence.scan_store import ANALYSIS_TYPE_STORAGE_SCAN


@dataclass(frozen=True)
class SourceAnalysis:
    id: int
    started_at: str
    target_path: str
    files_count: int
    total_bytes: int
    locations: tuple[str, ...]


@dataclass(frozen=True)
class DuplicateSearchRecord:
    id: int
    analysis_id: int
    status: str
    started_at: str
    finished_at: str | None
    candidates_count: int
    processed_count: int
    read_count: int
    reused_count: int
    bytes_read: int
    skipped_count: int
    groups_count: int | None
    group_files_count: int | None
    logical_bytes: int | None
    excess_copies: int | None
    recoverable_bytes: int | None
    hardlink_aliases: int | None
    elapsed_seconds: float | None
    error_message: str | None


@dataclass(frozen=True)
class DuplicateGroupRecord:
    id: int
    sha256: str
    size_bytes: int
    files_count: int
    physical_count: int
    excess_copies: int
    recoverable_bytes: int
    sample_name: str


@dataclass(frozen=True)
class DuplicateMemberRecord:
    name: str
    folder: str
    size_bytes: int
    modified_at: int | None
    sha256: str
    hash_reused: bool
    is_hardlink_alias: bool


_SEARCH_COLUMNS = """
    id, analysis_id, status, started_at, finished_at, candidates_count,
    processed_count, read_count, reused_count, bytes_read, skipped_count,
    groups_count, group_files_count, logical_bytes, excess_copies,
    recoverable_bytes, hardlink_aliases, elapsed_seconds, error_message
"""


class DuplicateRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def completed_analyses(self, limit: int = 200) -> list[SourceAnalysis]:
        """Análises de armazenamento concluídas, da mais recente para a mais antiga."""
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, target_path, files_count, total_bytes
                FROM analysis_history
                WHERE analysis_type = ? AND status = 'completed'
                ORDER BY started_at DESC, id DESC LIMIT ?
                """,
                (ANALYSIS_TYPE_STORAGE_SCAN, limit),
            ).fetchall()
            result = []
            for row in rows:
                paths = tuple(
                    r[0]
                    for r in conn.execute(
                        "SELECT path FROM analysis_locations WHERE analysis_id = ? ORDER BY id",
                        (row["id"],),
                    )
                )
                result.append(
                    SourceAnalysis(
                        row["id"],
                        row["started_at"],
                        row["target_path"],
                        row["files_count"] or 0,
                        row["total_bytes"] or 0,
                        paths,
                    )
                )
        return result

    def search(self, search_id: int) -> DuplicateSearchRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_SEARCH_COLUMNS} FROM duplicate_searches WHERE id = ?", (search_id,)
            ).fetchone()
        return DuplicateSearchRecord(**dict(row)) if row is not None else None

    def latest_completed(self) -> DuplicateSearchRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_SEARCH_COLUMNS} FROM duplicate_searches
                WHERE status = 'completed'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return DuplicateSearchRecord(**dict(row)) if row is not None else None

    def groups(self, search_id: int, limit: int = 1000) -> list[DuplicateGroupRecord]:
        """Grupos ordenados pelo maior espaço potencialmente recuperável."""
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT g.id, g.sha256, g.size_bytes, g.files_count, g.physical_count,
                       g.excess_copies, g.recoverable_bytes,
                       (SELECT f.name FROM duplicate_files df
                        JOIN indexed_files f ON f.id = df.file_id
                        WHERE df.group_id = g.id ORDER BY df.id LIMIT 1) AS sample_name
                FROM duplicate_groups g
                WHERE g.search_id = ?
                ORDER BY g.recoverable_bytes DESC, g.files_count DESC, g.id
                LIMIT ?
                """,
                (search_id, limit),
            ).fetchall()
        return [DuplicateGroupRecord(**dict(r)) for r in rows]

    def members(self, group_id: int) -> list[DuplicateMemberRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT f.name, d.path AS folder, df.size_bytes, f.modified_at, df.sha256,
                       df.hash_reused, df.is_hardlink_alias
                FROM duplicate_files df
                JOIN indexed_files f ON f.id = df.file_id
                JOIN indexed_folders d ON d.id = f.folder_id
                WHERE df.group_id = ?
                ORDER BY d.path, f.name
                """,
                (group_id,),
            ).fetchall()
        return [
            DuplicateMemberRecord(
                r["name"],
                r["folder"],
                r["size_bytes"],
                r["modified_at"],
                r["sha256"],
                bool(r["hash_reused"]),
                bool(r["is_hardlink_alias"]),
            )
            for r in rows
        ]

    def mark_interrupted(self) -> int:
        """Encerra buscas que ficaram 'em andamento' porque o aplicativo foi fechado."""
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE duplicate_searches
                SET status = 'failed', finished_at = ?,
                    error_message = 'Busca interrompida: o aplicativo foi encerrado.'
                WHERE status = 'running'
                """,
                (utc_now(),),
            )
            return cursor.rowcount
