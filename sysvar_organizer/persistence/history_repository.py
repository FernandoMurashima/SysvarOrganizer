"""Histórico consolidado das atividades SOMENTE LEITURA.

Reúne análises de armazenamento, buscas de duplicados, comparações de pastas,
buscas de fotografias semelhantes e simulações de organização. Planos e
operações sobre arquivos ficam em ``operations_store`` e são apresentados em
seção separada, para nunca misturar leitura com alteração de arquivos.

As consultas são paginadas e filtradas no próprio SQLite; nada é carregado
inteiro em memória.
"""

from __future__ import annotations

from dataclasses import dataclass

from sysvar_organizer.persistence.database import Database

KIND_STORAGE = "storage"
KIND_DUPLICATES = "duplicates"
KIND_COMPARISON = "comparison"
KIND_VISUAL = "visual"
KIND_ORGANIZATION = "organization"

ACTIVITY_LABELS = {
    KIND_STORAGE: "Análise de armazenamento",
    KIND_DUPLICATES: "Busca de duplicados",
    KIND_COMPARISON: "Comparação de pastas",
    KIND_VISUAL: "Fotografias semelhantes",
    KIND_ORGANIZATION: "Simulação de organização",
}

# Colunas: kind, id, started_at, finished_at, status, has_warnings, subject,
# m1, m2, m3, elapsed_seconds, error_message.
_ACTIVITY_SQL = f"""
    SELECT '{KIND_STORAGE}' AS kind, id, started_at, finished_at, status,
           has_warnings, target_path AS subject,
           files_count AS m1, total_bytes AS m2, folders_count AS m3,
           elapsed_seconds, error_message
    FROM analysis_history
    UNION ALL
    SELECT '{KIND_DUPLICATES}', s.id, s.started_at, s.finished_at, s.status, 0,
           COALESCE((SELECT a.target_path FROM analysis_history a WHERE a.id = s.analysis_id),
                    'análise nº ' || s.analysis_id),
           s.groups_count, s.recoverable_bytes, s.excess_copies,
           s.elapsed_seconds, s.error_message
    FROM duplicate_searches s
    UNION ALL
    SELECT '{KIND_COMPARISON}', id, started_at, finished_at, status, 0,
           root_a || '  ×  ' || root_b,
           identical_pairs, different_count, indeterminate_count,
           elapsed_seconds, error_message
    FROM folder_comparisons
    UNION ALL
    SELECT '{KIND_VISUAL}', id, started_at, finished_at, status, 0, root,
           groups_count, grouped_images, NULL,
           elapsed_seconds, error_message
    FROM visual_searches
    UNION ALL
    SELECT '{KIND_ORGANIZATION}', id, started_at, finished_at, status, 0,
           root || '  →  ' || destination,
           files_count, total_bytes, model,
           elapsed_seconds, error_message
    FROM organization_simulations
"""


@dataclass(frozen=True)
class ActivityFilter:
    """Filtros do histórico. ``start``/``end`` são instantes UTC em ISO 8601
    (início inclusivo, fim exclusivo), no mesmo formato gravado no banco."""

    kind: str | None = None
    status: str | None = None
    text: str = ""
    start: str | None = None
    end: str | None = None
    newest_first: bool = True

    @property
    def is_filtered(self) -> bool:
        return bool(self.kind or self.status or self.text.strip() or self.start or self.end)


@dataclass(frozen=True)
class ActivityRecord:
    kind: str
    id: int
    started_at: str
    finished_at: str | None
    status: str
    has_warnings: bool
    subject: str
    m1: int | None
    m2: int | None
    m3: object
    elapsed_seconds: float | None
    error_message: str | None

    @property
    def label(self) -> str:
        return ACTIVITY_LABELS.get(self.kind, self.kind)


def escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def activity_query(filters: ActivityFilter) -> tuple[str, tuple]:
    """SQL ordenado (sem LIMIT) e parâmetros para as atividades filtradas."""
    where: list[str] = []
    params: list = []
    if filters.kind:
        where.append("kind = ?")
        params.append(filters.kind)
    if filters.status:
        where.append("status = ?")
        params.append(filters.status)
    text = filters.text.strip()
    if text:
        clause = "subject LIKE ? ESCAPE '\\'"
        params.append(f"%{escape_like(text)}%")
        if text.isdigit():
            clause = f"({clause} OR id = ?)"
            params.append(int(text))
        where.append(clause)
    if filters.start:
        where.append("started_at >= ?")
        params.append(filters.start)
    if filters.end:
        where.append("started_at < ?")
        params.append(filters.end)
    order = "DESC" if filters.newest_first else "ASC"
    sql = f"SELECT * FROM ({_ACTIVITY_SQL})"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY started_at {order}, kind, id {order}"
    return sql, tuple(params)


class HistoryRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def count_activities(self, filters: ActivityFilter | None = None) -> int:
        sql, params = activity_query(filters or ActivityFilter())
        with self._db.connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0])

    def activities(
        self, offset: int, limit: int, filters: ActivityFilter | None = None
    ) -> list[ActivityRecord]:
        sql, params = activity_query(filters or ActivityFilter())
        with self._db.connection() as conn:
            rows = conn.execute(f"{sql} LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return [activity_record(r) for r in rows]

    def activity(self, kind: str, activity_id: int) -> ActivityRecord | None:
        sql = f"SELECT * FROM ({_ACTIVITY_SQL}) WHERE kind = ? AND id = ?"
        with self._db.connection() as conn:
            row = conn.execute(sql, (kind, activity_id)).fetchone()
        return activity_record(row) if row is not None else None


def activity_record(row) -> ActivityRecord:
    values = list(row)
    values[5] = bool(values[5])
    return ActivityRecord(*values)
