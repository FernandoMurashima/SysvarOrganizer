"""Gravação de uma busca de duplicados exatos.

Os candidatos são materializados em ``duplicate_files`` no início da busca,
para que o processamento percorra o banco em partes, sem manter a lista
completa em memória. Os grupos só são criados quando a busca termina sem
cancelamento nem falha: resultados parciais nunca são apresentados como
concluídos.

Cada instância usa uma conexão própria, aberta na thread do processamento.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.hash_cache import lookup_hash, store_hash
from sysvar_organizer.persistence.repositories import utc_now
from sysvar_organizer.persistence.scan_store import ANALYSIS_TYPE_STORAGE_SCAN


class SourceAnalysisError(ValueError):
    """A análise de origem não existe ou não está concluída."""


@dataclass(frozen=True)
class Candidate:
    row_id: int
    file_id: int
    name: str
    folder: str
    modified_at: int | None


@dataclass(frozen=True)
class GroupTotals:
    groups: int
    group_files: int
    logical_bytes: int
    excess_copies: int
    recoverable_bytes: int
    hardlink_aliases: int


class DuplicateStore:
    COMMIT_INTERVAL_SECONDS = 1.0
    MAX_RECORDED_ISSUES = 1000

    def __init__(self, database: Database) -> None:
        self._conn = database.open()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._last_commit = time.monotonic()
        self._issues_recorded = 0
        self.search_id: int | None = None

    # Início --------------------------------------------------------------

    def begin(self, analysis_id: int) -> int:
        """Registra a busca e seleciona os candidatos (tamanhos repetidos).

        Retorna a quantidade de candidatos.
        """
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
            INSERT INTO duplicate_searches (analysis_id, status, started_at)
            VALUES (?, 'running', ?)
            """,
            (analysis_id, utc_now()),
        )
        self.search_id = int(cursor.lastrowid)

        # Etapa A: somente arquivos cujo tamanho se repete na mesma análise.
        cursor = self._conn.execute(
            """
            INSERT INTO duplicate_files (search_id, file_id, size_bytes, status)
            SELECT ?, f.id, f.size_bytes, 'pending'
            FROM indexed_files f
            WHERE f.analysis_id = ?
              AND f.size_bytes IN (
                  SELECT size_bytes FROM indexed_files
                  WHERE analysis_id = ?
                  GROUP BY size_bytes HAVING COUNT(*) > 1
              )
            """,
            (self.search_id, analysis_id, analysis_id),
        )
        candidates = max(cursor.rowcount, 0)
        self._conn.execute(
            "UPDATE duplicate_searches SET candidates_count = ? WHERE id = ?",
            (candidates, self.search_id),
        )
        self.commit()
        return candidates

    # Leitura dos candidatos ---------------------------------------------

    def candidate_sizes(self) -> list[int]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT size_bytes FROM duplicate_files
            WHERE search_id = ? ORDER BY size_bytes
            """,
            (self.search_id,),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def candidates_of_size(self, size: int) -> list[Candidate]:
        rows = self._conn.execute(
            """
            SELECT df.id, df.file_id, f.name, d.path, f.modified_at
            FROM duplicate_files df
            JOIN indexed_files f ON f.id = df.file_id
            JOIN indexed_folders d ON d.id = f.folder_id
            WHERE df.search_id = ? AND df.size_bytes = ? AND df.status = 'pending'
            ORDER BY df.id
            """,
            (self.search_id, size),
        ).fetchall()
        return [Candidate(r[0], r[1], r[2], r[3], r[4]) for r in rows]

    # Cache de SHA-256 ----------------------------------------------------

    def cached_hash(
        self, path_key: str, size: int, modified_ns: int, volume_id: str, file_index: str
    ) -> str | None:
        """Hash anterior, somente se os metadados atuais coincidirem integralmente."""
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

    # Resultado por arquivo ---------------------------------------------

    def mark_hashed(self, row_id: int, sha256: str, reused: bool, physical_key: str) -> None:
        self._conn.execute(
            """
            UPDATE duplicate_files
            SET status = 'hashed', sha256 = ?, hash_reused = ?, physical_key = ?
            WHERE id = ?
            """,
            (sha256, int(reused), physical_key, row_id),
        )

    def mark_not_needed(self, row_id: int) -> None:
        self._conn.execute(
            "UPDATE duplicate_files SET status = 'not_needed' WHERE id = ?", (row_id,)
        )

    def mark_skipped(
        self, row_id: int, file_id: int, kind: str, path: str, message: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE duplicate_files SET status = 'skipped' WHERE id = ?", (row_id,)
        )
        if self._issues_recorded < self.MAX_RECORDED_ISSUES:
            self._conn.execute(
                """
                INSERT INTO duplicate_issues (search_id, file_id, kind, path, message, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (self.search_id, file_id, kind, path, message, utc_now()),
            )
            self._issues_recorded += 1

    def update_progress(
        self, processed: int, read_count: int, reused: int, bytes_read: int, skipped: int
    ) -> None:
        self._conn.execute(
            """
            UPDATE duplicate_searches
            SET processed_count = ?, read_count = ?, reused_count = ?,
                bytes_read = ?, skipped_count = ?
            WHERE id = ?
            """,
            (processed, read_count, reused, bytes_read, skipped, self.search_id),
        )

    # Etapa C: agrupamento ------------------------------------------------

    def build_groups(self) -> GroupTotals:
        """Agrupa por tamanho e SHA-256. Um grupo exige ao menos dois arquivos
        físicos distintos; caminhos do mesmo arquivo físico (hard links) não
        contam como cópias excedentes."""
        sid = self.search_id
        self._conn.execute(
            """
            INSERT INTO duplicate_groups (
                search_id, sha256, size_bytes, files_count, physical_count,
                excess_copies, recoverable_bytes
            )
            SELECT search_id, sha256, size_bytes, COUNT(*), COUNT(DISTINCT physical_key),
                   COUNT(DISTINCT physical_key) - 1,
                   (COUNT(DISTINCT physical_key) - 1) * size_bytes
            FROM duplicate_files
            WHERE search_id = ? AND status = 'hashed'
            GROUP BY size_bytes, sha256
            HAVING COUNT(DISTINCT physical_key) >= 2
            """,
            (sid,),
        )
        self._conn.execute(
            """
            UPDATE duplicate_files
            SET group_id = (
                SELECT g.id FROM duplicate_groups g
                WHERE g.search_id = duplicate_files.search_id
                  AND g.size_bytes = duplicate_files.size_bytes
                  AND g.sha256 = duplicate_files.sha256
            )
            WHERE search_id = ? AND status = 'hashed'
            """,
            (sid,),
        )
        # Em cada grupo, caminhos adicionais de um mesmo arquivo físico.
        self._conn.execute(
            """
            UPDATE duplicate_files
            SET is_hardlink_alias = 1
            WHERE search_id = ? AND group_id IS NOT NULL
              AND id > (
                  SELECT MIN(d2.id) FROM duplicate_files d2
                  WHERE d2.group_id = duplicate_files.group_id
                    AND d2.physical_key = duplicate_files.physical_key
              )
            """,
            (sid,),
        )
        row = self._conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(files_count), 0),
                   COALESCE(SUM(files_count * size_bytes), 0),
                   COALESCE(SUM(excess_copies), 0), COALESCE(SUM(recoverable_bytes), 0),
                   COALESCE(SUM(files_count - physical_count), 0)
            FROM duplicate_groups WHERE search_id = ?
            """,
            (sid,),
        ).fetchone()
        return GroupTotals(*(int(v) for v in row))

    # Encerramento --------------------------------------------------------

    def finish(
        self,
        status: str,
        *,
        processed: int,
        read_count: int,
        reused: int,
        bytes_read: int,
        skipped: int,
        totals: GroupTotals | None,
        elapsed_seconds: float,
        error_message: str | None,
    ) -> None:
        self.update_progress(processed, read_count, reused, bytes_read, skipped)
        values = (
            (
                totals.groups,
                totals.group_files,
                totals.logical_bytes,
                totals.excess_copies,
                totals.recoverable_bytes,
                totals.hardlink_aliases,
            )
            if totals is not None
            else (None,) * 6
        )
        self._conn.execute(
            """
            UPDATE duplicate_searches
            SET status = ?, finished_at = ?, groups_count = ?, group_files_count = ?,
                logical_bytes = ?, excess_copies = ?, recoverable_bytes = ?,
                hardlink_aliases = ?, elapsed_seconds = ?, error_message = ?
            WHERE id = ?
            """,
            (status, utc_now(), *values, round(elapsed_seconds, 3), error_message, self.search_id),
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
