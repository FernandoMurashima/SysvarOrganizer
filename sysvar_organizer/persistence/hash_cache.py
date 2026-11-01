"""Acesso à tabela ``file_hash_cache`` (SHA-256 já calculados).

Um hash só é devolvido quando todos os metadados informados coincidem com os
registrados; a revalidação dos arquivos é responsabilidade de quem consulta.
"""

from __future__ import annotations

import sqlite3

from sysvar_organizer.persistence.repositories import utc_now


def lookup_hash(
    conn: sqlite3.Connection,
    path_key: str,
    size: int,
    modified_ns: int,
    volume_id: str,
    file_index: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT sha256 FROM file_hash_cache
        WHERE path_key = ? AND size_bytes = ? AND modified_ns = ?
          AND volume_id = ? AND file_index = ?
        """,
        (path_key, size, modified_ns, volume_id, file_index),
    ).fetchone()
    return row[0] if row is not None else None


def store_hash(
    conn: sqlite3.Connection,
    path_key: str,
    size: int,
    modified_ns: int,
    volume_id: str,
    file_index: str,
    sha256: str,
) -> None:
    conn.execute(
        """
        INSERT INTO file_hash_cache
            (path_key, size_bytes, modified_ns, volume_id, file_index, sha256, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (path_key) DO UPDATE SET
            size_bytes = excluded.size_bytes,
            modified_ns = excluded.modified_ns,
            volume_id = excluded.volume_id,
            file_index = excluded.file_index,
            sha256 = excluded.sha256,
            computed_at = excluded.computed_at
        """,
        (path_key, size, modified_ns, volume_id, file_index, sha256, utc_now()),
    )
