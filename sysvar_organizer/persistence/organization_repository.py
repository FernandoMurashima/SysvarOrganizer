"""Consultas paginadas sobre simulações de organização."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.repositories import utc_now


@dataclass(frozen=True)
class SimulationRecord:
    id: int
    analysis_id: int
    root: str
    model: str
    destination: str
    exclude_sensitive: int
    rule: str
    alerts: str | None
    status: str
    started_at: str
    finished_at: str | None
    files_count: int
    processed_count: int
    total_bytes: int | None
    proposal_bytes: int | None
    ok_count: int | None
    already_count: int | None
    review_count: int | None
    conflict_count: int | None
    invalid_count: int | None
    excluded_count: int | None
    folders_count: int | None
    structures_count: int | None
    elapsed_seconds: float | None
    error_message: str | None


@dataclass(frozen=True)
class ItemRecord:
    id: int
    name: str
    folder: str
    full_path: str
    size_bytes: int
    modified_at: int | None
    category: str
    ambiguous: bool
    year: int | None
    month: int | None
    proposed_path: str | None
    status: str
    issues: tuple[str, ...]
    rule: str
    justification: str
    structure_path: str | None


_SIM_COLUMNS = ", ".join(f.name for f in fields(SimulationRecord))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class OrganizationRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def simulation(self, simulation_id: int) -> SimulationRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_SIM_COLUMNS} FROM organization_simulations WHERE id = ?",
                (simulation_id,),
            ).fetchone()
        return SimulationRecord(**dict(row)) if row is not None else None

    def latest_completed(self) -> SimulationRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_SIM_COLUMNS} FROM organization_simulations
                WHERE status = 'completed'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return SimulationRecord(**dict(row)) if row is not None else None

    def category_counts(self, simulation_id: int) -> list[tuple[str, int, int]]:
        """(categoria, arquivos, bytes), da maior para a menor quantidade."""
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT i.category, COUNT(*), COALESCE(SUM(f.size_bytes), 0)
                FROM organization_items i JOIN indexed_files f ON f.id = i.file_id
                WHERE i.simulation_id = ?
                GROUP BY i.category ORDER BY COUNT(*) DESC, i.category
                """,
                (simulation_id,),
            ).fetchall()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    def structures(self, simulation_id: int) -> list[tuple[str, str, str, int]]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT path, kind, reason, files_count FROM organization_structures
                WHERE simulation_id = ? ORDER BY path
                """,
                (simulation_id,),
            ).fetchall()
        return [(r[0], r[1], r[2], int(r[3])) for r in rows]

    def _item_filter(
        self, simulation_id: int, category: str | None, status: str | None, search: str | None
    ) -> tuple[str, tuple]:
        where = "i.simulation_id = ?"
        params: tuple = (simulation_id,)
        if category:
            where += " AND i.category = ?"
            params += (category,)
        if status:
            where += " AND i.status = ?"
            params += (status,)
        if search:
            where += " AND f.name LIKE ? ESCAPE '\\'"
            params += (f"%{_escape_like(search)}%",)
        return where, params

    def count_items(
        self,
        simulation_id: int,
        category: str | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> int:
        where, params = self._item_filter(simulation_id, category, status, search)
        with self._db.connection() as conn:
            return int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM organization_items i
                    JOIN indexed_files f ON f.id = i.file_id WHERE {where}
                    """,
                    params,
                ).fetchone()[0]
            )

    def items(
        self,
        simulation_id: int,
        category: str | None,
        status: str | None,
        search: str | None,
        offset: int,
        limit: int,
    ) -> list[ItemRecord]:
        where, params = self._item_filter(simulation_id, category, status, search)
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT i.id, f.name, d.path, f.size_bytes, f.modified_at, i.category,
                       i.ambiguous, i.year, i.month, i.proposed_path, i.status, i.issues,
                       i.rule, i.justification, i.structure_path
                FROM organization_items i
                JOIN indexed_files f ON f.id = i.file_id
                JOIN indexed_folders d ON d.id = f.folder_id
                WHERE {where}
                ORDER BY i.id
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [
            ItemRecord(
                id=r[0],
                name=r[1],
                folder=r[2],
                full_path=os.path.join(r[2], r[1]),
                size_bytes=r[3],
                modified_at=r[4],
                category=r[5],
                ambiguous=bool(r[6]),
                year=r[7],
                month=r[8],
                proposed_path=r[9],
                status=r[10],
                issues=tuple(code for code in (r[11] or "").split(",") if code),
                rule=r[12],
                justification=r[13],
                structure_path=r[14],
            )
            for r in rows
        ]

    def count_folders(self, simulation_id: int) -> int:
        with self._db.connection() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT proposed_folder) FROM organization_items
                    WHERE simulation_id = ? AND proposed_folder IS NOT NULL
                    """,
                    (simulation_id,),
                ).fetchone()[0]
            )

    def folders(self, simulation_id: int, offset: int, limit: int) -> list[tuple[str, int, int]]:
        """Estrutura proposta: (pasta relativa ao destino, arquivos, bytes)."""
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT i.proposed_folder, COUNT(*), COALESCE(SUM(f.size_bytes), 0)
                FROM organization_items i JOIN indexed_files f ON f.id = i.file_id
                WHERE i.simulation_id = ? AND i.proposed_folder IS NOT NULL
                GROUP BY i.proposed_folder ORDER BY i.proposed_folder
                LIMIT ? OFFSET ?
                """,
                (simulation_id, limit, offset),
            ).fetchall()
        return [(r[0], int(r[1]), int(r[2])) for r in rows]

    def mark_interrupted(self) -> int:
        """Encerra simulações que ficaram 'em andamento' com o aplicativo fechado."""
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE organization_simulations
                SET status = 'failed', finished_at = ?,
                    error_message = 'Simulação interrompida: o aplicativo foi encerrado.'
                WHERE status = 'running'
                """,
                (utc_now(),),
            )
            return cursor.rowcount
