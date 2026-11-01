"""Consultas paginadas sobre buscas de fotografias semelhantes."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, fields

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.repositories import utc_now


@dataclass(frozen=True)
class VisualSearchRecord:
    id: int
    analysis_id: int
    root: str
    status: str
    started_at: str
    finished_at: str | None
    criteria: str
    candidates_count: int
    processed_count: int
    computed_count: int
    reused_count: int
    skipped_count: int
    pairs_count: int | None
    groups_count: int | None
    grouped_images: int | None
    exact_groups: int | None
    very_high_groups: int | None
    probable_groups: int | None
    uncertain_groups: int | None
    elapsed_seconds: float | None
    error_message: str | None


@dataclass(frozen=True)
class VisualGroupRecord:
    id: int
    band: str
    members_count: int
    max_phash_distance: int
    max_dhash_distance: int
    exact_members: int
    representative_name: str
    representative_width: int
    representative_height: int


@dataclass(frozen=True)
class VisualMemberRecord:
    position: int
    name: str
    full_path: str
    size_bytes: int
    modified_at: int | None
    width: int
    height: int
    format: str
    phash_distance: int
    dhash_distance: int
    sha256: str | None
    is_exact_copy: bool
    same_physical: bool


_SEARCH_COLUMNS = ", ".join(f.name for f in fields(VisualSearchRecord))


class VisualRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    # Estimativa de candidatas -------------------------------------------

    def folder_paths(self, analysis_id: int) -> list[tuple[int, str]]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id, path FROM indexed_folders WHERE analysis_id = ?", (analysis_id,)
            ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def image_totals_by_folder(
        self, analysis_id: int, extensions: Sequence[str]
    ) -> dict[int, tuple[int, int]]:
        marks = ", ".join("?" for _ in extensions)
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT folder_id, COUNT(*), COALESCE(SUM(size_bytes), 0)
                FROM indexed_files
                WHERE analysis_id = ? AND size_bytes > 0 AND extension IN ({marks})
                GROUP BY folder_id
                """,
                (analysis_id, *extensions),
            ).fetchall()
        return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}

    # Buscas --------------------------------------------------------------

    def search(self, search_id: int) -> VisualSearchRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_SEARCH_COLUMNS} FROM visual_searches WHERE id = ?", (search_id,)
            ).fetchone()
        return VisualSearchRecord(**dict(row)) if row is not None else None

    def latest_completed(self) -> VisualSearchRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_SEARCH_COLUMNS} FROM visual_searches
                WHERE status = 'completed'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return VisualSearchRecord(**dict(row)) if row is not None else None

    def count_groups(self, search_id: int, band: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM visual_groups WHERE search_id = ?"
        params: tuple = (search_id,)
        if band:
            sql += " AND band = ?"
            params += (band,)
        with self._db.connection() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def groups(
        self, search_id: int, band: str | None, offset: int, limit: int
    ) -> list[VisualGroupRecord]:
        """Grupos do mais confiável para o menos confiável."""
        where = "g.search_id = ?"
        params: tuple = (search_id,)
        if band:
            where += " AND g.band = ?"
            params += (band,)
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT g.id, g.band, g.members_count, g.max_phash_distance,
                       g.max_dhash_distance, g.exact_members, f.name, v.width, v.height
                FROM visual_groups g
                JOIN visual_images v ON v.id = g.representative_id
                JOIN indexed_files f ON f.id = v.file_id
                WHERE {where}
                ORDER BY CASE g.band WHEN 'exact' THEN 0 WHEN 'very_high' THEN 1
                                     WHEN 'probable' THEN 2 ELSE 3 END,
                         g.max_phash_distance, g.id
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [VisualGroupRecord(*r) for r in rows]

    def members(self, group_id: int) -> list[VisualMemberRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT m.position, f.name, d.path, f.size_bytes, f.modified_at,
                       v.width, v.height, v.format, m.phash_distance, m.dhash_distance,
                       m.sha256, m.is_exact_copy, m.same_physical
                FROM visual_group_members m
                JOIN visual_images v ON v.id = m.image_id
                JOIN indexed_files f ON f.id = v.file_id
                JOIN indexed_folders d ON d.id = f.folder_id
                WHERE m.group_id = ?
                ORDER BY m.position
                """,
                (group_id,),
            ).fetchall()
        return [
            VisualMemberRecord(
                r[0], r[1], os.path.join(r[2], r[1]), r[3], r[4], r[5], r[6], r[7],
                r[8], r[9], r[10], bool(r[11]), bool(r[12]),
            )
            for r in rows
        ]

    def skipped_summary(self, search_id: int) -> dict[str, int]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT issue_kind, COUNT(*) FROM visual_images
                WHERE search_id = ? AND status = 'skipped' GROUP BY issue_kind
                """,
                (search_id,),
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def mark_interrupted(self) -> int:
        """Encerra buscas visuais que ficaram 'em andamento' com o aplicativo fechado."""
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE visual_searches
                SET status = 'failed', finished_at = ?,
                    error_message = 'Busca interrompida: o aplicativo foi encerrado.'
                WHERE status = 'running'
                """,
                (utc_now(),),
            )
            return cursor.rowcount
