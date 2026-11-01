"""Gravação de uma comparação de pastas.

Os arquivos dos conjuntos A e B são materializados em ``comparison_files`` no
início e percorridos em páginas, sem manter a lista completa em memória. A
classificação é feita em SQL, por junções indexadas (caminho relativo e
tamanho + SHA-256), nunca comparando cada arquivo com todos os demais.

Os resultados só são gravados quando a comparação termina sem cancelamento
nem falha. Cada instância usa uma conexão própria, aberta na thread do
processamento.
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

PAGE_SIZE = 2000

__all__ = ["ComparisonStore", "ComparisonTotals", "Entry", "SourceAnalysisError"]


@dataclass(frozen=True)
class Entry:
    id: int
    side: str
    size: int
    full_path: str
    modified_at: int | None


@dataclass(frozen=True)
class ComparisonTotals:
    identical_pairs: int
    identical_files_a: int
    identical_files_b: int
    renamed_pairs: int
    renamed_listed: int
    renamed_files_a: int
    renamed_files_b: int
    moved_pairs: int
    different_count: int
    only_a_count: int
    only_b_count: int
    indeterminate_count: int
    same_physical_pairs: int
    matched_files_a: int
    matched_files_b: int
    matched_physical: int
    shared_contents: int
    shared_bytes: int
    folders_both: int
    folders_only_a: int
    folders_only_b: int
    content_equivalent: bool
    structure_equivalent: bool


class ComparisonStore:
    COMMIT_INTERVAL_SECONDS = 1.0

    def __init__(self, database: Database) -> None:
        self._conn = database.open()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._last_commit = time.monotonic()
        self.comparison_id: int | None = None

    # Início --------------------------------------------------------------

    def begin(self, analysis_a: int, root_a: str, analysis_b: int, root_b: str) -> int:
        for analysis_id in {analysis_a, analysis_b}:
            row = self._conn.execute(
                "SELECT analysis_type, status FROM analysis_history WHERE id = ?",
                (analysis_id,),
            ).fetchone()
            if (
                row is None
                or row["analysis_type"] != ANALYSIS_TYPE_STORAGE_SCAN
                or row["status"] != "completed"
            ):
                raise SourceAnalysisError(
                    "Uma das análises de origem não existe ou não está concluída."
                )
        cursor = self._conn.execute(
            """
            INSERT INTO folder_comparisons
                (analysis_a_id, root_a, analysis_b_id, root_b, status, started_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (analysis_a, root_a, analysis_b, root_b, utc_now()),
        )
        self.comparison_id = int(cursor.lastrowid)
        self.commit()
        return self.comparison_id

    def analysis_folders(self, analysis_id: int) -> list[tuple[int, str]]:
        rows = self._conn.execute(
            "SELECT id, path FROM indexed_folders WHERE analysis_id = ?", (analysis_id,)
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def iter_analysis_files(self, analysis_id: int) -> Iterator[tuple[int, int, str, int]]:
        """(file_id, folder_id, nome, tamanho), em páginas por id."""
        last_id = 0
        while True:
            rows = self._conn.execute(
                """
                SELECT id, folder_id, name, size_bytes FROM indexed_files
                WHERE analysis_id = ? AND id > ? ORDER BY id LIMIT ?
                """,
                (analysis_id, last_id, PAGE_SIZE),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield int(row[0]), int(row[1]), row[2], int(row[3])
            last_id = int(rows[-1][0])

    def add_entries(self, rows: Sequence[tuple]) -> None:
        """Linhas: (lado, file_id, rel_path, rel_key, folder_key, name_key, tamanho)."""
        self._conn.executemany(
            """
            INSERT INTO comparison_files (
                comparison_id, side, file_id, rel_path, rel_key, folder_key, name_key,
                size_bytes, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            [(self.comparison_id, *row) for row in rows],
        )

    def add_folders(self, rows: Sequence[tuple[str, str]]) -> None:
        """Linhas: (caminho relativo, estado 'both' | 'only_a' | 'only_b')."""
        self._conn.executemany(
            "INSERT INTO comparison_folders (comparison_id, rel_path, status) VALUES (?, ?, ?)",
            [(self.comparison_id, rel, status) for rel, status in rows],
        )

    def set_file_counts(self, files_a: int, files_b: int) -> None:
        self._conn.execute(
            "UPDATE folder_comparisons SET files_a = ?, files_b = ? WHERE id = ?",
            (files_a, files_b, self.comparison_id),
        )

    # Leitura dos arquivos -------------------------------------------------

    def sizes_on_both_sides(self) -> set[int]:
        rows = self._conn.execute(
            """
            SELECT size_bytes FROM comparison_files WHERE comparison_id = ?
            GROUP BY size_bytes
            HAVING SUM(side = 'A') > 0 AND SUM(side = 'B') > 0
            """,
            (self.comparison_id,),
        ).fetchall()
        return {int(r[0]) for r in rows}

    def iter_entries_by_size(self) -> Iterator[Entry]:
        """Arquivos pendentes, ordenados por tamanho, em páginas (keyset)."""
        last_size, last_id = -1, 0
        while True:
            rows = self._conn.execute(
                """
                SELECT c.id, c.side, c.size_bytes, d.path, f.name, f.modified_at
                FROM comparison_files c
                JOIN indexed_files f ON f.id = c.file_id
                JOIN indexed_folders d ON d.id = f.folder_id
                WHERE c.comparison_id = ? AND c.status = 'pending'
                  AND (c.size_bytes > ? OR (c.size_bytes = ? AND c.id > ?))
                ORDER BY c.size_bytes, c.id
                LIMIT ?
                """,
                (self.comparison_id, last_size, last_size, last_id, PAGE_SIZE),
            ).fetchall()
            if not rows:
                return
            for r in rows:
                yield Entry(int(r[0]), r[1], int(r[2]), os.path.join(r[3], r[4]), r[5])
            last_size, last_id = int(rows[-1][2]), int(rows[-1][0])

    # Cache de SHA-256 ----------------------------------------------------

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

    # Resultado por arquivo ---------------------------------------------

    def mark_ok(self, entry_id: int, sha256: str | None, reused: bool, physical_key: str) -> None:
        self._conn.execute(
            """
            UPDATE comparison_files
            SET status = 'ok', sha256 = ?, hash_reused = ?, physical_key = ?
            WHERE id = ?
            """,
            (sha256, int(reused), physical_key, entry_id),
        )

    def mark_indeterminate(self, entry_id: int, kind: str, message: str) -> None:
        self._conn.execute(
            """
            UPDATE comparison_files
            SET status = 'indeterminate', issue_kind = ?, issue_message = ?
            WHERE id = ?
            """,
            (kind, message, entry_id),
        )

    def update_progress(self, processed: int, read_count: int, reused: int, bytes_read: int) -> None:
        self._conn.execute(
            """
            UPDATE folder_comparisons
            SET processed_count = ?, read_count = ?, reused_count = ?, bytes_read = ?
            WHERE id = ?
            """,
            (processed, read_count, reused, bytes_read, self.comparison_id),
        )

    # Classificação -------------------------------------------------------

    def classify(self, pair_cap: int) -> ComparisonTotals:
        cid = self.comparison_id
        q = self._conn.execute

        # Conteúdos presentes nos dois lados (tamanho + SHA-256 confirmados).
        q("DROP TABLE IF EXISTS temp.shared_content")
        q(
            """
            CREATE TEMP TABLE shared_content AS
            SELECT size_bytes, sha256, SUM(side = 'A') AS na, SUM(side = 'B') AS nb
            FROM comparison_files
            WHERE comparison_id = ? AND status = 'ok' AND sha256 IS NOT NULL
            GROUP BY size_bytes, sha256
            HAVING SUM(side = 'A') > 0 AND SUM(side = 'B') > 0
            """,
            (cid,),
        )

        # 1. Idênticos: mesmo caminho relativo e mesmo conteúdo.
        q(
            """
            INSERT INTO comparison_results
                (comparison_id, kind, a_entry_id, b_entry_id, size_bytes, same_physical)
            SELECT a.comparison_id, 'identical', a.id, b.id, a.size_bytes,
                   a.physical_key = b.physical_key
            FROM comparison_files a
            JOIN comparison_files b
              ON b.comparison_id = a.comparison_id AND b.side = 'B' AND b.rel_key = a.rel_key
            WHERE a.comparison_id = ? AND a.side = 'A'
              AND a.status = 'ok' AND b.status = 'ok'
              AND a.sha256 IS NOT NULL AND a.sha256 = b.sha256
              AND a.size_bytes = b.size_bytes
            """,
            (cid,),
        )

        # 2. Idênticos com nomes/locais diferentes: todas as correspondências,
        #    limitadas a pair_cap por conteúdo para não explodir em N x M.
        renamed_sql = """
            INSERT INTO comparison_results
                (comparison_id, kind, a_entry_id, b_entry_id, size_bytes,
                 same_physical, name_differs, folder_differs)
            SELECT a.comparison_id, 'identical_renamed', a.id, b.id, a.size_bytes,
                   a.physical_key = b.physical_key,
                   a.name_key != b.name_key, a.folder_key != b.folder_key
            FROM comparison_files a
            JOIN comparison_files b
              ON b.comparison_id = a.comparison_id AND b.side = 'B' AND b.status = 'ok'
             AND b.sha256 = a.sha256 AND b.size_bytes = a.size_bytes
            WHERE a.comparison_id = ? AND a.side = 'A' AND a.status = 'ok'
              AND a.rel_key != b.rel_key
        """
        q(
            renamed_sql
            + """
              AND EXISTS (SELECT 1 FROM shared_content s
                          WHERE s.size_bytes = a.size_bytes AND s.sha256 = a.sha256
                            AND s.na * s.nb <= ?)
            """,
            (cid, pair_cap),
        )
        large = q(
            "SELECT size_bytes, sha256 FROM shared_content WHERE na * nb > ?", (pair_cap,)
        ).fetchall()
        for size, sha in large:
            q(
                renamed_sql
                + " AND a.size_bytes = ? AND a.sha256 = ? ORDER BY a.id, b.id LIMIT ?",
                (cid, size, sha, pair_cap),
            )

        # 3. Diferentes: mesmo caminho relativo, conteúdo comprovadamente diferente.
        q(
            """
            INSERT INTO comparison_results
                (comparison_id, kind, a_entry_id, b_entry_id, size_bytes, same_physical)
            SELECT a.comparison_id, 'different', a.id, b.id, a.size_bytes,
                   a.physical_key = b.physical_key
            FROM comparison_files a
            JOIN comparison_files b
              ON b.comparison_id = a.comparison_id AND b.side = 'B' AND b.rel_key = a.rel_key
            WHERE a.comparison_id = ? AND a.side = 'A'
              AND a.status = 'ok' AND b.status = 'ok'
              AND (a.size_bytes != b.size_bytes
                   OR (a.sha256 IS NOT NULL AND b.sha256 IS NOT NULL AND a.sha256 != b.sha256))
            """,
            (cid,),
        )

        # 4. Indeterminados com par pelo caminho relativo.
        q(
            """
            INSERT INTO comparison_results
                (comparison_id, kind, a_entry_id, b_entry_id, size_bytes, detail)
            SELECT a.comparison_id, 'indeterminate', a.id, b.id, a.size_bytes,
                   CASE
                     WHEN a.status = 'indeterminate' AND b.status = 'indeterminate'
                       THEN 'A: ' || a.issue_message || '  ·  B: ' || b.issue_message
                     WHEN a.status = 'indeterminate' THEN 'A: ' || a.issue_message
                     WHEN b.status = 'indeterminate' THEN 'B: ' || b.issue_message
                     ELSE 'Conteúdo não verificado.'
                   END
            FROM comparison_files a
            JOIN comparison_files b
              ON b.comparison_id = a.comparison_id AND b.side = 'B' AND b.rel_key = a.rel_key
            WHERE a.comparison_id = ? AND a.side = 'A'
              AND (a.status != 'ok' OR b.status != 'ok'
                   OR (a.size_bytes = b.size_bytes
                       AND (a.sha256 IS NULL OR b.sha256 IS NULL)))
            """,
            (cid,),
        )

        # 5. Indeterminados sem par pelo caminho relativo.
        for side, other, column in (("A", "B", "a_entry_id"), ("B", "A", "b_entry_id")):
            q(
                f"""
                INSERT INTO comparison_results
                    (comparison_id, kind, {column}, size_bytes, detail)
                SELECT e.comparison_id, 'indeterminate', e.id, e.size_bytes,
                       '{side}: ' || e.issue_message
                FROM comparison_files e
                WHERE e.comparison_id = ? AND e.side = ? AND e.status = 'indeterminate'
                  AND NOT EXISTS (SELECT 1 FROM comparison_files o
                                  WHERE o.comparison_id = e.comparison_id AND o.side = ?
                                    AND o.rel_key = e.rel_key)
                """,
                (cid, side, other),
            )

        # 6. Sem correspondência: exclusivo, a menos que um arquivo indeterminado
        #    do outro lado (mesmo tamanho indexado) possa ser o correspondente.
        for side, other, column, kind in (
            ("A", "B", "a_entry_id", "only_a"),
            ("B", "A", "b_entry_id", "only_b"),
        ):
            q(
                f"""
                INSERT INTO comparison_results
                    (comparison_id, kind, {column}, size_bytes, detail)
                SELECT e.comparison_id,
                       CASE WHEN EXISTS (
                                SELECT 1 FROM comparison_files o
                                WHERE o.comparison_id = e.comparison_id AND o.side = ?
                                  AND o.status = 'indeterminate'
                                  AND o.size_bytes = e.size_bytes)
                            THEN 'indeterminate' ELSE '{kind}' END,
                       e.id, e.size_bytes,
                       CASE WHEN EXISTS (
                                SELECT 1 FROM comparison_files o
                                WHERE o.comparison_id = e.comparison_id AND o.side = ?
                                  AND o.status = 'indeterminate'
                                  AND o.size_bytes = e.size_bytes)
                            THEN 'Possível correspondência em {other} não pôde ser verificada.'
                            ELSE NULL END
                FROM comparison_files e
                WHERE e.comparison_id = ? AND e.side = ? AND e.status = 'ok'
                  AND NOT EXISTS (SELECT 1 FROM comparison_files o
                                  WHERE o.comparison_id = e.comparison_id AND o.side = ?
                                    AND o.rel_key = e.rel_key)
                  AND NOT (e.sha256 IS NOT NULL AND EXISTS (
                                SELECT 1 FROM comparison_files o
                                WHERE o.comparison_id = e.comparison_id AND o.side = ?
                                  AND o.status = 'ok' AND o.sha256 = e.sha256
                                  AND o.size_bytes = e.size_bytes))
                """,
                (other, other, cid, side, other, other),
            )

        totals = self._totals()
        q("DROP TABLE IF EXISTS temp.shared_content")
        return totals

    def _totals(self) -> ComparisonTotals:
        cid = self.comparison_id

        def one(sql: str, params: tuple = ()) -> int:
            value = self._conn.execute(sql, (cid, *params)).fetchone()[0]
            return int(value or 0)

        kinds = dict(
            self._conn.execute(
                """
                SELECT kind, COUNT(*) FROM comparison_results
                WHERE comparison_id = ? GROUP BY kind
                """,
                (cid,),
            ).fetchall()
        )
        identical_pairs = int(kinds.get("identical", 0))
        identical_files_a = one(
            "SELECT COUNT(DISTINCT a_entry_id) FROM comparison_results "
            "WHERE comparison_id = ? AND kind = 'identical'"
        )
        identical_files_b = one(
            "SELECT COUNT(DISTINCT b_entry_id) FROM comparison_results "
            "WHERE comparison_id = ? AND kind = 'identical'"
        )
        shared = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0), COALESCE(SUM(na * nb), 0), "
            "COALESCE(SUM(na), 0), COALESCE(SUM(nb), 0) FROM shared_content"
        ).fetchone()
        shared_contents, shared_bytes, content_pairs, matched_a, matched_b = (int(v) for v in shared)

        renamed_files = {}
        for side, other in (("A", "B"), ("B", "A")):
            renamed_files[side] = one(
                """
                SELECT COUNT(*) FROM comparison_files e
                WHERE e.comparison_id = ? AND e.side = ? AND e.status = 'ok'
                  AND e.sha256 IS NOT NULL
                  AND EXISTS (SELECT 1 FROM comparison_files o
                              WHERE o.comparison_id = e.comparison_id AND o.side = ?
                                AND o.status = 'ok' AND o.sha256 = e.sha256
                                AND o.size_bytes = e.size_bytes AND o.rel_key != e.rel_key)
                """,
                (side, other),
            )
        matched_physical = int(
            self._conn.execute(
                """
                SELECT COUNT(DISTINCT e.physical_key) FROM comparison_files e
                JOIN shared_content s ON s.size_bytes = e.size_bytes AND s.sha256 = e.sha256
                WHERE e.comparison_id = ? AND e.status = 'ok'
                """,
                (cid,),
            ).fetchone()[0]
        )
        folders = dict(
            self._conn.execute(
                "SELECT status, COUNT(*) FROM comparison_folders WHERE comparison_id = ? "
                "GROUP BY status",
                (cid,),
            ).fetchall()
        )
        counts = self._conn.execute(
            "SELECT files_a, files_b FROM folder_comparisons WHERE id = ?", (cid,)
        ).fetchone()
        files_a, files_b = int(counts[0]), int(counts[1])
        status_counts = dict(
            self._conn.execute(
                "SELECT side || status, COUNT(*) FROM comparison_files WHERE comparison_id = ? "
                "GROUP BY side, status",
                (cid,),
            ).fetchall()
        )
        ok_a, ok_b = int(status_counts.get("Aok", 0)), int(status_counts.get("Bok", 0))
        indeterminate_files = int(status_counts.get("Aindeterminate", 0)) + int(
            status_counts.get("Bindeterminate", 0)
        )

        content_equivalent = (
            indeterminate_files == 0 and ok_a == matched_a and ok_b == matched_b
        )
        structure_equivalent = (
            indeterminate_files == 0
            and int(folders.get("only_a", 0)) == 0
            and int(folders.get("only_b", 0)) == 0
            and identical_files_a == files_a
            and identical_files_b == files_b
        )
        return ComparisonTotals(
            identical_pairs=identical_pairs,
            identical_files_a=identical_files_a,
            identical_files_b=identical_files_b,
            renamed_pairs=content_pairs - identical_pairs,
            renamed_listed=int(kinds.get("identical_renamed", 0)),
            renamed_files_a=renamed_files["A"],
            renamed_files_b=renamed_files["B"],
            moved_pairs=one(
                "SELECT COUNT(*) FROM comparison_results WHERE comparison_id = ? "
                "AND kind = 'identical_renamed' AND folder_differs = 1"
            ),
            different_count=int(kinds.get("different", 0)),
            only_a_count=int(kinds.get("only_a", 0)),
            only_b_count=int(kinds.get("only_b", 0)),
            indeterminate_count=int(kinds.get("indeterminate", 0)),
            same_physical_pairs=one(
                "SELECT COUNT(*) FROM comparison_results WHERE comparison_id = ? "
                "AND kind IN ('identical', 'identical_renamed') AND same_physical = 1"
            ),
            matched_files_a=matched_a,
            matched_files_b=matched_b,
            matched_physical=matched_physical,
            shared_contents=shared_contents,
            shared_bytes=shared_bytes,
            folders_both=int(folders.get("both", 0)),
            folders_only_a=int(folders.get("only_a", 0)),
            folders_only_b=int(folders.get("only_b", 0)),
            content_equivalent=content_equivalent,
            structure_equivalent=structure_equivalent,
        )

    # Encerramento --------------------------------------------------------

    def finish(
        self,
        status: str,
        *,
        processed: int,
        read_count: int,
        reused: int,
        bytes_read: int,
        totals: ComparisonTotals | None,
        elapsed_seconds: float,
        error_message: str | None,
    ) -> None:
        self.update_progress(processed, read_count, reused, bytes_read)
        assignments = ""
        values: list = []
        if totals is not None:
            data = totals.__dict__
            assignments = ", " + ", ".join(f"{name} = ?" for name in data)
            values = [int(v) if isinstance(v, bool) else v for v in data.values()]
        self._conn.execute(
            f"""
            UPDATE folder_comparisons
            SET status = ?, finished_at = ?, elapsed_seconds = ?, error_message = ?{assignments}
            WHERE id = ?
            """,
            (status, utc_now(), round(elapsed_seconds, 3), error_message, *values, self.comparison_id),
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
