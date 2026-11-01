"""Histórico consolidado das atividades SOMENTE LEITURA.

Reúne análises de armazenamento, buscas de duplicados, comparações de pastas,
buscas de fotografias semelhantes e simulações de organização. Planos e
operações sobre arquivos ficam em ``operations_store`` e são apresentados em
seção separada, para nunca misturar leitura com alteração de arquivos.
"""

from __future__ import annotations

from dataclasses import dataclass

from sysvar_organizer.persistence.database import Database

_ACTIVITY_SQL = """
    SELECT 'Análise de armazenamento' AS kind, id, started_at, status,
           target_path AS subject,
           COALESCE(files_count, 0) || ' arquivos' AS summary
    FROM analysis_history
    UNION ALL
    SELECT 'Busca de duplicados', id, started_at, status, 'análise nº ' || analysis_id,
           COALESCE(groups_count, 0) || ' grupos'
    FROM duplicate_searches
    UNION ALL
    SELECT 'Comparação de pastas', id, started_at, status, root_a || '  ×  ' || root_b,
           COALESCE(identical_pairs, 0) || ' idênticos'
    FROM folder_comparisons
    UNION ALL
    SELECT 'Fotografias semelhantes', id, started_at, status, root,
           COALESCE(groups_count, 0) || ' grupos'
    FROM visual_searches
    UNION ALL
    SELECT 'Simulação de organização', id, started_at, status, root,
           files_count || ' arquivos · modelo ' || model
    FROM organization_simulations
"""


@dataclass(frozen=True)
class ActivityRecord:
    kind: str
    id: int
    started_at: str
    status: str
    subject: str
    summary: str


class HistoryRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def count_activities(self) -> int:
        with self._db.connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM ({_ACTIVITY_SQL})").fetchone()[0])

    def activities(self, offset: int, limit: int) -> list[ActivityRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM ({_ACTIVITY_SQL}) ORDER BY started_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [ActivityRecord(*r) for r in rows]
