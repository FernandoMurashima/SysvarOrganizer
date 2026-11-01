"""Consultas paginadas sobre comparações de pastas."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.repositories import utc_now


@dataclass(frozen=True)
class ComparisonRecord:
    id: int
    analysis_a_id: int
    root_a: str
    analysis_b_id: int
    root_b: str
    status: str
    started_at: str
    finished_at: str | None
    files_a: int
    files_b: int
    processed_count: int
    read_count: int
    reused_count: int
    bytes_read: int
    identical_pairs: int | None
    identical_files_a: int | None
    identical_files_b: int | None
    renamed_pairs: int | None
    renamed_listed: int | None
    renamed_files_a: int | None
    renamed_files_b: int | None
    moved_pairs: int | None
    different_count: int | None
    only_a_count: int | None
    only_b_count: int | None
    indeterminate_count: int | None
    same_physical_pairs: int | None
    matched_files_a: int | None
    matched_files_b: int | None
    matched_physical: int | None
    shared_contents: int | None
    shared_bytes: int | None
    folders_both: int | None
    folders_only_a: int | None
    folders_only_b: int | None
    content_equivalent: int | None
    structure_equivalent: int | None
    elapsed_seconds: float | None
    error_message: str | None


@dataclass(frozen=True)
class FileSide:
    rel_path: str
    full_path: str
    size_bytes: int
    sha256: str | None
    modified_at: int | None


@dataclass(frozen=True)
class ResultRow:
    id: int
    kind: str
    size_bytes: int | None
    same_physical: bool
    name_differs: bool
    folder_differs: bool
    detail: str | None
    a: FileSide | None
    b: FileSide | None


_RECORD_COLUMNS = ", ".join(f.name for f in fields(ComparisonRecord))

_SIDE_COLUMNS = """
    {p}.rel_path, d{p}.path, i{p}.name, {p}.size_bytes, {p}.sha256, i{p}.modified_at
"""


def _side(row, offset: int) -> FileSide | None:
    if row[offset] is None:
        return None
    rel, folder, name, size, sha, modified = row[offset : offset + 6]
    return FileSide(rel, os.path.join(folder, name), size, sha, modified)


class ComparisonRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # Conjuntos -----------------------------------------------------------

    def folder_paths(self, analysis_id: int) -> list[tuple[int, str]]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id, path FROM indexed_folders WHERE analysis_id = ?", (analysis_id,)
            ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def file_totals_by_folder(self, analysis_id: int) -> dict[int, tuple[int, int]]:
        """folder_id -> (arquivos, bytes)."""
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT folder_id, COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM indexed_files WHERE analysis_id = ? GROUP BY folder_id
                """,
                (analysis_id,),
            ).fetchall()
        return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}

    # Comparações ---------------------------------------------------------

    def comparison(self, comparison_id: int) -> ComparisonRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_RECORD_COLUMNS} FROM folder_comparisons WHERE id = ?",
                (comparison_id,),
            ).fetchone()
        return ComparisonRecord(**dict(row)) if row is not None else None

    def latest_completed(self) -> ComparisonRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_RECORD_COLUMNS} FROM folder_comparisons
                WHERE status = 'completed'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return ComparisonRecord(**dict(row)) if row is not None else None

    def count_results(self, comparison_id: int, kind: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM comparison_results WHERE comparison_id = ?"
        params: tuple = (comparison_id,)
        if kind:
            sql += " AND kind = ?"
            params += (kind,)
        with self._db.connection() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def results(
        self, comparison_id: int, kind: str | None, offset: int, limit: int
    ) -> list[ResultRow]:
        where = "r.comparison_id = ?"
        params: tuple = (comparison_id,)
        if kind:
            where += " AND r.kind = ?"
            params += (kind,)
        sql = f"""
            SELECT r.id, r.kind, r.size_bytes, r.same_physical, r.name_differs,
                   r.folder_differs, r.detail,
                   {_SIDE_COLUMNS.format(p='a')},
                   {_SIDE_COLUMNS.format(p='b')}
            FROM comparison_results r
            LEFT JOIN comparison_files a ON a.id = r.a_entry_id
            LEFT JOIN indexed_files ia ON ia.id = a.file_id
            LEFT JOIN indexed_folders da ON da.id = ia.folder_id
            LEFT JOIN comparison_files b ON b.id = r.b_entry_id
            LEFT JOIN indexed_files ib ON ib.id = b.file_id
            LEFT JOIN indexed_folders db ON db.id = ib.folder_id
            WHERE {where}
            ORDER BY r.id
            LIMIT ? OFFSET ?
        """
        with self._db.connection() as conn:
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()
        return [
            ResultRow(
                id=r[0],
                kind=r[1],
                size_bytes=r[2],
                same_physical=bool(r[3]),
                name_differs=bool(r[4]),
                folder_differs=bool(r[5]),
                detail=r[6],
                a=_side(r, 7),
                b=_side(r, 13),
            )
            for r in rows
        ]

    def count_folders(self, comparison_id: int, status: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM comparison_folders WHERE comparison_id = ?"
        params: tuple = (comparison_id,)
        if status:
            sql += " AND status = ?"
            params += (status,)
        with self._db.connection() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def folders(
        self, comparison_id: int, status: str | None, offset: int, limit: int
    ) -> list[tuple[str, str]]:
        sql = "SELECT rel_path, status FROM comparison_folders WHERE comparison_id = ?"
        params: tuple = (comparison_id,)
        if status:
            sql += " AND status = ?"
            params += (status,)
        sql += " ORDER BY rel_path LIMIT ? OFFSET ?"
        with self._db.connection() as conn:
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def mark_interrupted(self) -> int:
        """Encerra comparações que ficaram 'em andamento' porque o aplicativo foi fechado."""
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE folder_comparisons
                SET status = 'failed', finished_at = ?,
                    error_message = 'Comparação interrompida: o aplicativo foi encerrado.'
                WHERE status = 'running'
                """,
                (utc_now(),),
            )
            return cursor.rowcount
