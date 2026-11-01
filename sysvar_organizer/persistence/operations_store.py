"""Planos de operação, itens, revisões, execuções, eventos e recuperações.

As mudanças de estado são feitas por comparação e troca
(``UPDATE ... WHERE state IN (...)``): uma ação só passa a "em execução" se
ainda estiver no estado esperado, o que impede execuções duplicadas por
repetição de comandos. Cada chamada usa uma transação curta e própria, para
que o estado seja registrado ANTES de qualquer operação física.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.repositories import utc_now

ACTIVE_FOR_APPROVAL = ("planned", "validated")


@dataclass(frozen=True)
class PlanRecord:
    id: int
    plan_key: str
    simulation_id: int | None
    analysis_id: int | None
    environment: str
    test_root: str | None
    operation: str
    destination: str
    status: str
    created_at: str
    updated_at: str
    items_count: int


@dataclass(frozen=True)
class OperationItem:
    id: int
    plan_id: int
    action_key: str
    unit_key: str
    file_id: int | None
    source_path: str
    destination_path: str | None
    action_type: str
    justification: str
    size_bytes: int
    expected_mtime: int | None
    sensitive: int
    alerts: str
    dependencies: str
    block_reason: str | None
    state: str
    approved: int
    reversible: int
    src_volume: str | None
    src_index: str | None
    src_mtime_ns: int | None
    sha256: str | None
    dst_volume: str | None
    dst_index: str | None
    dst_mtime_ns: int | None
    updated_at: str


@dataclass(frozen=True)
class SourceRow:
    """Linha da prévia do Bloco 06 usada para montar o plano."""

    org_item_id: int
    file_id: int
    folder: str
    name: str
    size_bytes: int
    modified_at: int | None
    category: str
    proposed_path: str | None
    status: str
    issues: str
    justification: str
    structure_path: str | None


_PLAN_COLUMNS = ", ".join(f.name for f in fields(PlanRecord))
_ITEM_COLUMNS = ", ".join(f.name for f in fields(OperationItem))
_ITEM_INSERT = [
    f.name for f in fields(OperationItem) if f.name not in ("id", "updated_at")
]


class OperationsStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    # Fontes do plano -----------------------------------------------------

    def simulation_info(self, simulation_id: int) -> dict | None:
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT id, analysis_id, root, destination, status FROM organization_simulations "
                "WHERE id = ?",
                (simulation_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def simulation_rows(self, simulation_id: int) -> list[SourceRow]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT i.id, i.file_id, d.path, f.name, f.size_bytes, f.modified_at,
                       i.category, i.proposed_path, i.status, i.issues, i.justification,
                       i.structure_path
                FROM organization_items i
                JOIN indexed_files f ON f.id = i.file_id
                JOIN indexed_folders d ON d.id = f.folder_id
                WHERE i.simulation_id = ?
                ORDER BY i.id
                """,
                (simulation_id,),
            ).fetchall()
        return [SourceRow(*row) for row in rows]

    def duplicate_peers(self, analysis_id: int) -> dict[int, int]:
        """file_id -> quantidade de outros integrantes no grupo de duplicados exatos
        da busca concluída mais recente da mesma análise (somente informativo)."""
        with self._db.connection() as conn:
            search = conn.execute(
                "SELECT id FROM duplicate_searches WHERE analysis_id = ? AND status = 'completed' "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (analysis_id,),
            ).fetchone()
            if search is None:
                return {}
            rows = conn.execute(
                """
                SELECT df.file_id, g.files_count FROM duplicate_files df
                JOIN duplicate_groups g ON g.id = df.group_id
                WHERE df.search_id = ?
                """,
                (search[0],),
            ).fetchall()
        return {int(r[0]): int(r[1]) - 1 for r in rows}

    def visual_peers(self, analysis_id: int) -> dict[int, tuple[str, int]]:
        """file_id -> (faixa, integrantes) da busca visual concluída mais recente."""
        with self._db.connection() as conn:
            search = conn.execute(
                "SELECT id FROM visual_searches WHERE analysis_id = ? AND status = 'completed' "
                "ORDER BY started_at DESC, id DESC LIMIT 1",
                (analysis_id,),
            ).fetchone()
            if search is None:
                return {}
            rows = conn.execute(
                """
                SELECT v.file_id, g.band, g.members_count FROM visual_group_members m
                JOIN visual_groups g ON g.id = m.group_id
                JOIN visual_images v ON v.id = m.image_id
                WHERE m.search_id = ?
                """,
                (search[0],),
            ).fetchall()
        return {int(r[0]): (r[1], int(r[2])) for r in rows}

    # Planos ----------------------------------------------------------------

    def create_plan(
        self,
        plan_key: str,
        simulation_id: int | None,
        analysis_id: int | None,
        environment: str,
        test_root: str | None,
        operation: str,
        destination: str,
        items: Sequence[dict],
    ) -> int:
        """Cria o plano e seus itens numa única transação."""
        now = utc_now()
        with self._db.connection() as conn:
            existing = conn.execute(
                "SELECT id FROM operation_plans WHERE plan_key = ?", (plan_key,)
            ).fetchone()
            if existing is not None:  # idempotente: a mesma chave não cria outro plano
                return int(existing[0])
            cursor = conn.execute(
                """
                INSERT INTO operation_plans (
                    plan_key, simulation_id, analysis_id, environment, test_root, operation,
                    destination, status, created_at, updated_at, items_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    plan_key, simulation_id, analysis_id, environment, test_root, operation,
                    destination, now, now, len(items),
                ),
            )
            plan_id = int(cursor.lastrowid)
            columns = ["plan_id", *(c for c in _ITEM_INSERT if c != "plan_id"), "updated_at"]
            marks = ", ".join("?" for _ in columns)
            conn.executemany(
                f"INSERT INTO operation_items ({', '.join(columns)}) VALUES ({marks})",
                [
                    (plan_id, *(item.get(c) for c in columns[1:-1]), now)
                    for item in items
                ],
            )
            conn.execute(
                """
                INSERT INTO operation_events (plan_id, occurred_at, level, code, message)
                VALUES (?, ?, 'info', 'plan_created', ?)
                """,
                (plan_id, now, f"Plano criado com {len(items)} ação(ões); nenhuma aprovada."),
            )
        return plan_id

    def plan(self, plan_id: int) -> PlanRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_PLAN_COLUMNS} FROM operation_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        return PlanRecord(**dict(row)) if row is not None else None

    def plans(self, offset: int = 0, limit: int = 200) -> list[PlanRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_PLAN_COLUMNS} FROM operation_plans ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [PlanRecord(**dict(r)) for r in rows]

    def count_plans(self) -> int:
        with self._db.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM operation_plans").fetchone()[0])

    def cancel_plan(self, plan_id: int) -> bool:
        with self._db.connection() as conn:
            cursor = conn.execute(
                "UPDATE operation_plans SET status = 'cancelled', updated_at = ? "
                "WHERE id = ? AND status = 'open'",
                (utc_now(), plan_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE operation_items SET approved = 0 WHERE plan_id = ? "
                    "AND state IN ('planned', 'validated', 'blocked')",
                    (plan_id,),
                )
                self._event(conn, plan_id, None, "info", "plan_cancelled",
                            "Planejamento cancelado; nenhuma ação será executada.")
            return bool(cursor.rowcount)

    # Itens -----------------------------------------------------------------

    def _item_filter(self, plan_id: int, state: str | None, action: str | None) -> tuple[str, tuple]:
        where = "plan_id = ?"
        params: tuple = (plan_id,)
        if state:
            where += " AND state = ?"
            params += (state,)
        if action:
            where += " AND action_type = ?"
            params += (action,)
        return where, params

    def count_items(self, plan_id: int, state: str | None = None, action: str | None = None) -> int:
        where, params = self._item_filter(plan_id, state, action)
        with self._db.connection() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) FROM operation_items WHERE {where}", params).fetchone()[0]
            )

    def items(
        self,
        plan_id: int,
        state: str | None = None,
        action: str | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> list[OperationItem]:
        where, params = self._item_filter(plan_id, state, action)
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM operation_items WHERE {where} "
                "ORDER BY id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [OperationItem(**dict(r)) for r in rows]

    def item(self, item_id: int) -> OperationItem | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM operation_items WHERE id = ?", (item_id,)
            ).fetchone()
        return OperationItem(**dict(row)) if row is not None else None

    def unit_members(self, plan_id: int, unit_key: str) -> list[OperationItem]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM operation_items WHERE plan_id = ? AND unit_key = ? "
                "ORDER BY id",
                (plan_id, unit_key),
            ).fetchall()
        return [OperationItem(**dict(r)) for r in rows]

    def state_counts(self, plan_id: int) -> dict[str, int]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) FROM operation_items WHERE plan_id = ? GROUP BY state",
                (plan_id,),
            ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def approved_count(self, plan_id: int) -> int:
        with self._db.connection() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM operation_items WHERE plan_id = ? AND approved = 1",
                    (plan_id,),
                ).fetchone()[0]
            )

    # Revisão humana --------------------------------------------------------

    def set_unit_approval(self, plan_id: int, unit_key: str, approved: bool) -> int:
        """Aprova ou rejeita uma unidade inteira. Itens bloqueados nunca são aprovados.

        Retorna a quantidade de itens alterados (0 se o plano não estiver aberto
        ou se algum item da unidade estiver bloqueado, no caso de aprovação).
        """
        now = utc_now()
        with self._db.connection() as conn:
            plan = conn.execute(
                "SELECT status FROM operation_plans WHERE id = ?", (plan_id,)
            ).fetchone()
            if plan is None or plan[0] != "open":
                return 0
            states = [
                r[0]
                for r in conn.execute(
                    "SELECT state FROM operation_items WHERE plan_id = ? AND unit_key = ?",
                    (plan_id, unit_key),
                )
            ]
            if approved and any(s not in ACTIVE_FOR_APPROVAL for s in states):
                return 0
            allowed = ACTIVE_FOR_APPROVAL if approved else (*ACTIVE_FOR_APPROVAL, "blocked")
            marks = ", ".join("?" for _ in allowed)
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM operation_items WHERE plan_id = ? AND unit_key = ? "
                    f"AND state IN ({marks}) AND approved != ?",
                    (plan_id, unit_key, *allowed, int(approved)),
                )
            ]
            for item_id in ids:
                conn.execute(
                    "UPDATE operation_items SET approved = ?, updated_at = ? WHERE id = ?",
                    (int(approved), now, item_id),
                )
                conn.execute(
                    "INSERT INTO operation_reviews (item_id, decision, decided_at) VALUES (?, ?, ?)",
                    (item_id, "approved" if approved else "rejected", now),
                )
            return len(ids)

    # Estados ---------------------------------------------------------------

    def transition(
        self, item_id: int, from_states: Iterable[str], to_state: str, **values
    ) -> bool:
        """Troca de estado somente se o item estiver em um dos estados esperados."""
        from_states = tuple(from_states)
        assignments = ", ".join(["state = ?", "updated_at = ?", *(f"{k} = ?" for k in values)])
        marks = ", ".join("?" for _ in from_states)
        with self._db.connection() as conn:
            cursor = conn.execute(
                f"UPDATE operation_items SET {assignments} WHERE id = ? AND state IN ({marks})",
                (to_state, utc_now(), *values.values(), item_id, *from_states),
            )
            return cursor.rowcount == 1

    def update_item(self, item_id: int, **values) -> None:
        assignments = ", ".join(["updated_at = ?", *(f"{k} = ?" for k in values)])
        with self._db.connection() as conn:
            conn.execute(
                f"UPDATE operation_items SET {assignments} WHERE id = ?",
                (utc_now(), *values.values(), item_id),
            )

    def mark_interrupted(self) -> int:
        """Ações que estavam em execução quando o aplicativo terminou."""
        now = utc_now()
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id, plan_id FROM operation_items WHERE state IN ('running', 'revert_pending')"
            ).fetchall()
            for item_id, plan_id in rows:
                conn.execute(
                    "UPDATE operation_items SET state = 'interrupted', updated_at = ? WHERE id = ?",
                    (now, item_id),
                )
                self._event(conn, plan_id, item_id, "error", "interrupted",
                            "Execução interrompida; reconciliação obrigatória antes de nova tentativa.")
            conn.execute(
                "UPDATE operation_executions SET finished_at = ?, result = 'interrupted' "
                "WHERE finished_at IS NULL",
                (now,),
            )
            conn.execute(
                "UPDATE operation_recoveries SET finished_at = ?, result = 'failed', "
                "message = 'Interrompida' WHERE finished_at IS NULL",
                (now,),
            )
            return len(rows)

    # Registros -------------------------------------------------------------

    @staticmethod
    def _event(conn, plan_id: int, item_id: int | None, level: str, code: str, message: str) -> None:
        conn.execute(
            """
            INSERT INTO operation_events (plan_id, item_id, occurred_at, level, code, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (plan_id, item_id, utc_now(), level, code, message),
        )

    def add_event(self, plan_id: int, item_id: int | None, level: str, code: str, message: str) -> None:
        with self._db.connection() as conn:
            self._event(conn, plan_id, item_id, level, code, message)

    def start_execution(self, item_id: int, kind: str, prior_state: str) -> int:
        with self._db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO operation_executions (item_id, kind, prior_state, started_at) "
                "VALUES (?, ?, ?, ?)",
                (item_id, kind, prior_state, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_execution(self, execution_id: int, result: str, method: str | None, message: str) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "UPDATE operation_executions SET finished_at = ?, result = ?, method = ?, "
                "message = ? WHERE id = ?",
                (utc_now(), result, method, message, execution_id),
            )

    def start_recovery(self, item_id: int) -> int:
        with self._db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO operation_recoveries (item_id, requested_at) VALUES (?, ?)",
                (item_id, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_recovery(self, recovery_id: int, result: str, message: str) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "UPDATE operation_recoveries SET finished_at = ?, result = ?, message = ? "
                "WHERE id = ?",
                (utc_now(), result, message, recovery_id),
            )

    def events(self, plan_id: int, offset: int = 0, limit: int = 200) -> list[tuple]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT e.occurred_at, e.level, e.code, e.message, i.source_path
                FROM operation_events e LEFT JOIN operation_items i ON i.id = e.item_id
                WHERE e.plan_id = ? ORDER BY e.id DESC LIMIT ? OFFSET ?
                """,
                (plan_id, limit, offset),
            ).fetchall()
        return [tuple(r) for r in rows]

    def count_events(self, plan_id: int) -> int:
        with self._db.connection() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM operation_events WHERE plan_id = ?", (plan_id,)
                ).fetchone()[0]
            )

    def executions(self, item_id: int) -> list[tuple]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT kind, prior_state, started_at, finished_at, result, method, message "
                "FROM operation_executions WHERE item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [tuple(r) for r in rows]

    def recoveries(self, item_id: int) -> list[tuple]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT requested_at, finished_at, result, message FROM operation_recoveries "
                "WHERE item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [tuple(r) for r in rows]


def item_name(item: OperationItem) -> str:
    return os.path.basename(item.source_path)
