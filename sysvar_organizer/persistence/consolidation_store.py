"""Registro persistente da Consolidação de unidades.

Um ``job`` reúne a origem (unidade a esvaziar), o destino principal (que é
preservado), a prévia e a execução. Cada arquivo da origem é um item com
classificação, hash, destino previsto, estado e motivo. As trocas de estado
são feitas por comparação (``UPDATE ... WHERE state IN (...)``) e gravadas
ANTES de qualquer operação física.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, fields

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.hash_cache import lookup_hash, store_hash
from sysvar_organizer.persistence.repositories import utc_now

CLASS_TRANSFER = "transfer"
CLASS_IN_DESTINATION = "in_destination"
CLASS_SOURCE_DUPLICATE = "source_duplicate"
CLASS_PENDING = "pending"
STATE_EXCLUDED = "excluded"


@dataclass(frozen=True)
class JobRecord:
    id: int
    source_analysis_id: int
    source_root: str
    dest_analysis_id: int
    dest_root: str
    dest_base: str
    status: str
    created_at: str
    updated_at: str
    analyzed_at: str | None
    source_volume: str | None
    source_dir_index: str | None
    dest_volume: str | None
    dest_dir_index: str | None
    authorized_at: str | None
    files_total: int
    transfer_count: int
    transfer_bytes: int
    in_dest_count: int
    in_dest_bytes: int
    source_dup_count: int
    source_dup_bytes: int
    conflict_count: int
    pending_count: int
    source_issues: int
    dest_issues: int
    dest_unverified: int
    hashed_count: int
    reused_count: int
    bytes_read: int
    message: str | None


@dataclass(frozen=True)
class ItemRecord:
    id: int
    job_id: int
    file_id: int | None
    source_path: str
    rel_path: str
    rel_key: str
    size_bytes: int
    modified_at: int | None
    sha256: str | None
    classification: str | None
    match_path: str | None
    representative_id: int | None
    dest_path: str | None
    renamed: int
    state: str
    reason: str | None
    src_volume: str | None
    src_index: str | None
    src_mtime_ns: int | None
    dst_volume: str | None
    dst_index: str | None
    updated_at: str


_JOB_COLUMNS = ", ".join(f.name for f in fields(JobRecord))
_ITEM_COLUMNS = ", ".join(f.name for f in fields(ItemRecord))


class BulkHashCache:
    """Adaptador do cache de SHA-256 sobre uma conexão própria (análise)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def cached_hash(self, path_key, size, modified_ns, volume_id, file_index):
        return lookup_hash(self._conn, path_key, size, modified_ns, volume_id, file_index)

    def save_hash(self, path_key, size, modified_ns, volume_id, file_index, sha256):
        store_hash(self._conn, path_key, size, modified_ns, volume_id, file_index, sha256)


class ConsolidationStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    @property
    def database(self) -> Database:
        return self._db

    # Jobs ------------------------------------------------------------------

    def create_job(
        self, source_analysis_id: int, source_root: str, dest_analysis_id: int, dest_root: str,
        dest_base: str,
    ) -> int:
        now = utc_now()
        with self._db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO consolidation_jobs (source_analysis_id, source_root, dest_analysis_id, "
                "dest_root, dest_base, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'analyzing', ?, ?)",
                (source_analysis_id, source_root, dest_analysis_id, dest_root, dest_base, now, now),
            )
            return int(cursor.lastrowid)

    def job(self, job_id: int) -> JobRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM consolidation_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return JobRecord(**dict(row)) if row is not None else None

    def jobs(self, limit: int = 100) -> list[JobRecord]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM consolidation_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [JobRecord(**dict(r)) for r in rows]

    def update_job(self, job_id: int, **values) -> None:
        assignments = ", ".join(["updated_at = ?", *(f"{k} = ?" for k in values)])
        with self._db.connection() as conn:
            conn.execute(
                f"UPDATE consolidation_jobs SET {assignments} WHERE id = ?",
                (utc_now(), *values.values(), job_id),
            )

    def set_job_status(self, job_id: int, from_statuses: Iterable[str], status: str, **values) -> bool:
        from_statuses = tuple(from_statuses)
        assignments = ", ".join(["status = ?", "updated_at = ?", *(f"{k} = ?" for k in values)])
        marks = ", ".join("?" for _ in from_statuses)
        with self._db.connection() as conn:
            cursor = conn.execute(
                f"UPDATE consolidation_jobs SET {assignments} WHERE id = ? AND status IN ({marks})",
                (status, utc_now(), *values.values(), job_id, *from_statuses),
            )
            return cursor.rowcount == 1

    # Itens -----------------------------------------------------------------

    def item(self, item_id: int) -> ItemRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM consolidation_items WHERE id = ?", (item_id,)
            ).fetchone()
        return ItemRecord(**dict(row)) if row is not None else None

    def _filter(self, job_id: int, classification, state) -> tuple[str, tuple]:
        where, params = "job_id = ?", [job_id]
        for column, value in (("classification", classification), ("state", state)):
            if value is None:
                continue
            values = (value,) if isinstance(value, str) else tuple(value)
            where += f" AND {column} IN ({', '.join('?' for _ in values)})"
            params.extend(values)
        return where, tuple(params)

    def count_items(self, job_id: int, classification=None, state=None) -> int:
        where, params = self._filter(job_id, classification, state)
        with self._db.connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM consolidation_items WHERE {where}", params).fetchone()[0])

    def items(self, job_id: int, classification=None, state=None, offset: int = 0, limit: int = 200) -> list[ItemRecord]:
        where, params = self._filter(job_id, classification, state)
        with self._db.connection() as conn:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM consolidation_items WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [ItemRecord(**dict(r)) for r in rows]

    def iter_items(self, job_id: int, classification=None, state=None, batch: int = 500) -> Iterator[ItemRecord]:
        """Percorre itens em páginas por id (sem carregar tudo em memória)."""
        where, params = self._filter(job_id, classification, state)
        last = 0
        while True:
            with self._db.connection() as conn:
                rows = conn.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM consolidation_items WHERE {where} AND id > ? "
                    "ORDER BY id LIMIT ?",
                    (*params, last, batch),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield ItemRecord(**dict(row))
            last = int(rows[-1]["id"])

    def item_by_key(self, job_id: int, rel_key: str) -> ItemRecord | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM consolidation_items WHERE job_id = ? AND rel_key = ?",
                (job_id, rel_key),
            ).fetchone()
        return ItemRecord(**dict(row)) if row is not None else None

    def summary(self, job_id: int) -> dict[tuple[str | None, str], tuple[int, int]]:
        """(classificação, estado) -> (arquivos, bytes)."""
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT classification, state, COUNT(*), COALESCE(SUM(size_bytes), 0) "
                "FROM consolidation_items WHERE job_id = ? GROUP BY classification, state",
                (job_id,),
            ).fetchall()
        return {(r[0], r[1]): (int(r[2]), int(r[3])) for r in rows}

    def transition(self, item_id: int, from_states: Iterable[str], to_state: str, **values) -> bool:
        from_states = tuple(from_states)
        assignments = ", ".join(["state = ?", "updated_at = ?", *(f"{k} = ?" for k in values)])
        marks = ", ".join("?" for _ in from_states)
        with self._db.connection() as conn:
            cursor = conn.execute(
                f"UPDATE consolidation_items SET {assignments} WHERE id = ? AND state IN ({marks})",
                (to_state, utc_now(), *values.values(), item_id, *from_states),
            )
            return cursor.rowcount == 1

    def reset_items(self, job_id: int, classification: str, from_states: Sequence[str]) -> int:
        marks = ", ".join("?" for _ in from_states)
        with self._db.connection() as conn:
            cursor = conn.execute(
                f"UPDATE consolidation_items SET state = 'planned', reason = NULL, updated_at = ? "
                f"WHERE job_id = ? AND classification = ? AND state IN ({marks})",
                (utc_now(), job_id, classification, *from_states),
            )
            return cursor.rowcount

    # Eventos ----------------------------------------------------------------

    def add_event(self, job_id: int, item_id: int | None, level: str, code: str, message: str) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "INSERT INTO consolidation_events (job_id, item_id, occurred_at, level, code, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, item_id, utc_now(), level, code, message),
            )

    def events(self, job_id: int, limit: int = 200) -> list[tuple]:
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT occurred_at, level, code, message, item_id FROM consolidation_events "
                "WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return [tuple(r) for r in rows]

    # Recuperação ------------------------------------------------------------

    def mark_interrupted(self) -> int:
        """Itens e jobs que estavam em andamento quando o aplicativo terminou."""
        now = utc_now()
        with self._db.connection() as conn:
            items = conn.execute(
                "UPDATE consolidation_items SET state = 'interrupted', updated_at = ? "
                "WHERE state = 'running'",
                (now,),
            ).rowcount
            conn.execute(
                "UPDATE consolidation_jobs SET status = 'interrupted', updated_at = ?, "
                "message = 'Aplicativo encerrado durante a consolidação; retomada disponível.' "
                "WHERE status IN ('running', 'analyzing')",
                (now,),
            )
        return int(items)
