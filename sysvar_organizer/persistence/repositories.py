"""Repositórios de leitura e escrita sobre as tabelas do banco local."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sysvar_organizer.persistence.database import Database


def utc_now() -> str:
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
                (key, json.dumps(value), utc_now()),
            )


@dataclass(frozen=True)
class AnalysisRecord:
    id: int
    analysis_type: str
    target_path: str
    status: str
    started_at: str
    finished_at: str | None
    files_count: int | None
    folders_count: int | None
    total_bytes: int | None
    inaccessible_count: int | None
    skipped_count: int | None
    has_warnings: bool
    elapsed_seconds: float | None
    error_message: str | None


@dataclass(frozen=True)
class LocationRecord:
    path: str
    kind: str
    status: str
    files_count: int
    folders_count: int
    total_bytes: int


_ANALYSIS_COLUMNS = """
    id, analysis_type, target_path, status, started_at, finished_at,
    files_count, folders_count, total_bytes, inaccessible_count,
    skipped_count, has_warnings, elapsed_seconds, error_message
"""


def _to_record(row: Any) -> AnalysisRecord:
    data = dict(row)
    data["has_warnings"] = bool(data["has_warnings"])
    return AnalysisRecord(**data)


class AnalysisHistoryRepository:
    """Consultas ao histórico de análises."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def count(self) -> int:
        with self._db.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM analysis_history").fetchone()[0])

    def latest(self) -> AnalysisRecord | None:
        """Última análise registrada, em qualquer estado."""
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_ANALYSIS_COLUMNS} FROM analysis_history
                ORDER BY started_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return _to_record(row) if row is not None else None

    def latest_completed(self) -> AnalysisRecord | None:
        """Última análise concluída (com ou sem avisos)."""
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_ANALYSIS_COLUMNS} FROM analysis_history
                WHERE status = 'completed'
                ORDER BY started_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
        return _to_record(row) if row is not None else None

    def locations(self, analysis_id: int) -> list[LocationRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT path, kind, status, files_count, folders_count, total_bytes
                FROM analysis_locations WHERE analysis_id = ? ORDER BY id
                """,
                (analysis_id,),
            ).fetchall()
        return [LocationRecord(**dict(row)) for row in rows]

    def mark_interrupted(self) -> int:
        """Encerra análises que ficaram 'em andamento' porque o aplicativo foi fechado.

        Retorna a quantidade de análises ajustadas.
        """
        with self._db.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE analysis_history
                SET status = 'failed',
                    finished_at = ?,
                    error_message = 'Análise interrompida: o aplicativo foi encerrado.'
                WHERE status = 'running'
                """,
                (utc_now(),),
            )
            conn.execute(
                "UPDATE analysis_locations SET status = 'failed' WHERE status = 'pending'"
            )
            return cursor.rowcount
