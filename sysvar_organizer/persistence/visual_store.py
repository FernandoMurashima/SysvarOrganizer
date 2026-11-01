"""Gravação de uma busca de fotografias semelhantes.

As imagens candidatas são materializadas em ``visual_images`` e percorridas
em páginas. Os grupos só são confirmados quando a busca termina sem
cancelamento nem falha: durante o agrupamento nada é confirmado no banco
antes do encerramento. Cada instância usa uma conexão própria, aberta na
thread do processamento.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.duplicate_store import SourceAnalysisError
from sysvar_organizer.persistence.hash_cache import lookup_hash, store_hash
from sysvar_organizer.persistence.repositories import utc_now
from sysvar_organizer.persistence.scan_store import ANALYSIS_TYPE_STORAGE_SCAN

PAGE_SIZE = 1000

__all__ = ["CachedSignature", "ImageEntry", "SourceAnalysisError", "VisualStore"]


@dataclass(frozen=True)
class ImageEntry:
    id: int
    file_id: int
    full_path: str
    size: int
    modified_at: int | None


@dataclass(frozen=True)
class CachedSignature:
    phash: str
    dhash: str
    width: int
    height: int
    format: str


@dataclass(frozen=True)
class OkImage:
    id: int
    phash: str
    dhash: str
    width: int
    height: int
    size: int
    full_path: str
    modified_at: int | None
    physical_key: str


class VisualStore:
    COMMIT_INTERVAL_SECONDS = 1.0

    def __init__(self, database: Database) -> None:
        self._conn = database.open()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._last_commit = time.monotonic()
        self.search_id: int | None = None

    # Início --------------------------------------------------------------

    def begin(self, analysis_id: int, root: str, criteria: str) -> int:
        row = self._conn.execute(
            "SELECT analysis_type, status FROM analysis_history WHERE id = ?", (analysis_id,)
        ).fetchone()
        if (
            row is None
            or row["analysis_type"] != ANALYSIS_TYPE_STORAGE_SCAN
            or row["status"] != "completed"
        ):
            raise SourceAnalysisError("A análise de origem não existe ou não está concluída.")
        cursor = self._conn.execute(
            """
            INSERT INTO visual_searches (analysis_id, root, status, started_at, criteria)
            VALUES (?, ?, 'running', ?, ?)
            """,
            (analysis_id, root, utc_now(), criteria),
        )
        self.search_id = int(cursor.lastrowid)
        self.commit()
        return self.search_id

    def analysis_folders(self, analysis_id: int) -> list[tuple[int, str]]:
        rows = self._conn.execute(
            "SELECT id, path FROM indexed_folders WHERE analysis_id = ?", (analysis_id,)
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def iter_image_files(
        self, analysis_id: int, extensions: Sequence[str]
    ) -> Iterator[tuple[int, int]]:
        """(file_id, folder_id) dos arquivos com extensão de imagem e tamanho > 0."""
        marks = ", ".join("?" for _ in extensions)
        last_id = 0
        while True:
            rows = self._conn.execute(
                f"""
                SELECT id, folder_id FROM indexed_files
                WHERE analysis_id = ? AND id > ? AND size_bytes > 0
                  AND extension IN ({marks})
                ORDER BY id LIMIT ?
                """,
                (analysis_id, last_id, *extensions, PAGE_SIZE),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield int(row[0]), int(row[1])
            last_id = int(rows[-1][0])

    def add_candidates(self, file_ids: Sequence[int]) -> None:
        self._conn.executemany(
            "INSERT INTO visual_images (search_id, file_id, status) VALUES (?, ?, 'pending')",
            [(self.search_id, file_id) for file_id in file_ids],
        )

    def set_candidates(self, count: int) -> None:
        self._conn.execute(
            "UPDATE visual_searches SET candidates_count = ? WHERE id = ?",
            (count, self.search_id),
        )

    # Assinaturas ---------------------------------------------------------

    def iter_pending(self) -> Iterator[ImageEntry]:
        last_id = 0
        while True:
            rows = self._conn.execute(
                """
                SELECT v.id, v.file_id, d.path, f.name, f.size_bytes, f.modified_at
                FROM visual_images v
                JOIN indexed_files f ON f.id = v.file_id
                JOIN indexed_folders d ON d.id = f.folder_id
                WHERE v.search_id = ? AND v.status = 'pending' AND v.id > ?
                ORDER BY v.id LIMIT ?
                """,
                (self.search_id, last_id, PAGE_SIZE),
            ).fetchall()
            if not rows:
                return
            for r in rows:
                yield ImageEntry(int(r[0]), int(r[1]), os.path.join(r[2], r[3]), int(r[4]), r[5])
            last_id = int(rows[-1][0])

    def cached_signature(
        self,
        path_key: str,
        size: int,
        modified_ns: int,
        volume_id: str,
        file_index: str,
        version: str,
    ) -> CachedSignature | None:
        """Assinatura anterior, somente se identidade, metadados e versão coincidirem."""
        row = self._conn.execute(
            """
            SELECT phash, dhash, width, height, format FROM image_signatures
            WHERE path_key = ? AND size_bytes = ? AND modified_ns = ?
              AND volume_id = ? AND file_index = ? AND version = ?
            """,
            (path_key, size, modified_ns, volume_id, file_index, version),
        ).fetchone()
        return CachedSignature(*row) if row is not None else None

    def save_signature(
        self,
        path_key: str,
        size: int,
        modified_ns: int,
        volume_id: str,
        file_index: str,
        version: str,
        signature: CachedSignature,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO image_signatures (
                path_key, size_bytes, modified_ns, volume_id, file_index, version,
                phash, dhash, width, height, format, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (path_key) DO UPDATE SET
                size_bytes = excluded.size_bytes, modified_ns = excluded.modified_ns,
                volume_id = excluded.volume_id, file_index = excluded.file_index,
                version = excluded.version, phash = excluded.phash, dhash = excluded.dhash,
                width = excluded.width, height = excluded.height, format = excluded.format,
                computed_at = excluded.computed_at
            """,
            (
                path_key,
                size,
                modified_ns,
                volume_id,
                file_index,
                version,
                signature.phash,
                signature.dhash,
                signature.width,
                signature.height,
                signature.format,
                utc_now(),
            ),
        )

    # Cache de SHA-256 (cópias exatas) -----------------------------------

    def cached_hash(
        self, path_key: str, size: int, modified_ns: int, volume_id: str, file_index: str
    ) -> str | None:
        return lookup_hash(self._conn, path_key, size, modified_ns, volume_id, file_index)

    def save_hash(
        self,
        path_key: str,
        size: int,
        modified_ns: int,
        volume_id: str,
        file_index: str,
        sha256: str,
    ) -> None:
        store_hash(self._conn, path_key, size, modified_ns, volume_id, file_index, sha256)

    # Resultado por imagem ------------------------------------------------

    def mark_ok(
        self, image_id: int, signature: CachedSignature, reused: bool, physical_key: str
    ) -> None:
        self._conn.execute(
            """
            UPDATE visual_images
            SET status = 'ok', phash = ?, dhash = ?, width = ?, height = ?, format = ?,
                signature_reused = ?, physical_key = ?
            WHERE id = ?
            """,
            (
                signature.phash,
                signature.dhash,
                signature.width,
                signature.height,
                signature.format,
                int(reused),
                physical_key,
                image_id,
            ),
        )

    def mark_skipped(self, image_id: int, kind: str, message: str) -> None:
        self._conn.execute(
            """
            UPDATE visual_images SET status = 'skipped', issue_kind = ?, issue_message = ?
            WHERE id = ?
            """,
            (kind, message, image_id),
        )

    def update_progress(self, processed: int, computed: int, reused: int, skipped: int) -> None:
        self._conn.execute(
            """
            UPDATE visual_searches
            SET processed_count = ?, computed_count = ?, reused_count = ?, skipped_count = ?
            WHERE id = ?
            """,
            (processed, computed, reused, skipped, self.search_id),
        )

    # Agrupamento ---------------------------------------------------------

    def ok_images(self) -> list[OkImage]:
        rows = self._conn.execute(
            """
            SELECT v.id, v.phash, v.dhash, v.width, v.height, f.size_bytes, d.path, f.name,
                   f.modified_at, v.physical_key
            FROM visual_images v
            JOIN indexed_files f ON f.id = v.file_id
            JOIN indexed_folders d ON d.id = f.folder_id
            WHERE v.search_id = ? AND v.status = 'ok'
            ORDER BY v.id
            """,
            (self.search_id,),
        ).fetchall()
        return [
            OkImage(
                int(r[0]), r[1], r[2], int(r[3]), int(r[4]), int(r[5]),
                os.path.join(r[6], r[7]), r[8], r[9],
            )
            for r in rows
        ]

    def add_group(
        self,
        band: str,
        members_count: int,
        max_phash: int,
        max_dhash: int,
        exact_members: int,
        representative_id: int,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO visual_groups (
                search_id, band, members_count, max_phash_distance, max_dhash_distance,
                exact_members, representative_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.search_id,
                band,
                members_count,
                max_phash,
                max_dhash,
                exact_members,
                representative_id,
            ),
        )
        return int(cursor.lastrowid)

    def add_members(self, rows: Sequence[tuple]) -> None:
        """Linhas: (group_id, image_id, posição, dist. pHash, dist. dHash, sha256,
        cópia exata, mesmo arquivo físico)."""
        self._conn.executemany(
            """
            INSERT INTO visual_group_members (
                group_id, search_id, image_id, position, phash_distance, dhash_distance,
                sha256, is_exact_copy, same_physical
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(row[0], self.search_id, *row[1:]) for row in rows],
        )

    # Encerramento --------------------------------------------------------

    def finish(
        self,
        status: str,
        *,
        processed: int,
        computed: int,
        reused: int,
        skipped: int,
        totals: dict | None,
        elapsed_seconds: float,
        error_message: str | None,
    ) -> None:
        self.update_progress(processed, computed, reused, skipped)
        assignments = ""
        values: list = []
        if totals is not None:
            assignments = ", " + ", ".join(f"{name} = ?" for name in totals)
            values = list(totals.values())
        self._conn.execute(
            f"""
            UPDATE visual_searches
            SET status = ?, finished_at = ?, elapsed_seconds = ?, error_message = ?{assignments}
            WHERE id = ?
            """,
            (status, utc_now(), round(elapsed_seconds, 3), error_message, *values, self.search_id),
        )
        self.commit()

    def maybe_commit(self) -> None:
        if time.monotonic() - self._last_commit >= self.COMMIT_INTERVAL_SECONDS:
            self.commit()

    def commit(self) -> None:
        self._conn.commit()
        self._last_commit = time.monotonic()

    def discard_pending(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()
