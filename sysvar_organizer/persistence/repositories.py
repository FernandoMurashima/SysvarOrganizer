"""Repositórios de leitura e escrita sobre as tabelas do banco local."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sysvar_organizer.persistence.database import Database


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SettingsRepository:
    """Configurações chave/valor; os valores são armazenados como JSON."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, key: str, default: Any = None) -> Any:
        with self._db.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set(self, key: str, value: Any) -> None:
        with self._db.connection() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), _utc_now()),
            )


@dataclass(frozen=True)
class AnalysisRecord:
    id: int
    analysis_type: str
    status: str
    started_at: str
    finished_at: str | None
    files_count: int | None
    total_bytes: int | None


class AnalysisHistoryRepository:
    """Consulta ao histórico de análises. Nesta versão nenhuma análise é gravada."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def count(self) -> int:
        with self._db.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM analysis_history").fetchone()[0])

    def latest(self) -> AnalysisRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, analysis_type, status, started_at, finished_at,
                       files_count, total_bytes
                FROM analysis_history
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return AnalysisRecord(**dict(row)) if row is not None else None
