"""Gravação de uma simulação de organização (somente registros no banco local).

Nada é criado, movido ou alterado no disco: esta classe grava apenas a
classificação e os caminhos propostos. Cada instância usa uma conexão própria,
aberta na thread do processamento.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

from sysvar_organizer.persistence.database import Database
from sysvar_organizer.persistence.duplicate_store import SourceAnalysisError
from sysvar_organizer.persistence.repositories import utc_now
from sysvar_organizer.persistence.scan_store import ANALYSIS_TYPE_STORAGE_SCAN

PAGE_SIZE = 2000

__all__ = ["OrganizationStore", "SourceAnalysisError"]


class OrganizationStore:
    COMMIT_INTERVAL_SECONDS = 1.0

    def __init__(self, database: Database) -> None:
        self._conn = database.open()
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._last_commit = time.monotonic()
        self.simulation_id: int | None = None

    # Início --------------------------------------------------------------

    def begin(
        self,
        analysis_id: int,
        root: str,
        model: str,
        destination: str,
        exclude_sensitive: bool,
        rule: str,
        alerts: str,
    ) -> int:
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
            INSERT INTO organization_simulations (
                analysis_id, root, model, destination, exclude_sensitive, rule, alerts,
                status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (analysis_id, root, model, destination, int(exclude_sensitive), rule, alerts, utc_now()),
        )
        self.simulation_id = int(cursor.lastrowid)
        self.commit()
        return self.simulation_id

    def analysis_folders(self, analysis_id: int) -> list[tuple[int, str]]:
        rows = self._conn.execute(
            "SELECT id, path FROM indexed_folders WHERE analysis_id = ?", (analysis_id,)
        ).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def iter_analysis_files(
        self, analysis_id: int
    ) -> Iterator[tuple[int, int, str, str, int, int | None]]:
        """(file_id, folder_id, nome, extensão, tamanho, data de modificação), em páginas."""
        last_id = 0
        while True:
            rows = self._conn.execute(
                """
                SELECT id, folder_id, name, extension, size_bytes, modified_at
                FROM indexed_files
                WHERE analysis_id = ? AND id > ? ORDER BY id LIMIT ?
                """,
                (analysis_id, last_id, PAGE_SIZE),
            ).fetchall()
            if not rows:
                return
            for r in rows:
                yield int(r[0]), int(r[1]), r[2], r[3], int(r[4]), r[5]
            last_id = int(rows[-1][0])

    def set_files_count(self, count: int) -> None:
        self._conn.execute(
            "UPDATE organization_simulations SET files_count = ? WHERE id = ?",
            (count, self.simulation_id),
        )

    def add_structures(self, rows: Sequence[tuple[str, str, str, int]]) -> None:
        """Linhas: (caminho, tipo, motivo, arquivos)."""
        self._conn.executemany(
            """
            INSERT INTO organization_structures (simulation_id, path, kind, reason, files_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(self.simulation_id, *row) for row in rows],
        )

    def add_items(self, rows: Sequence[tuple]) -> None:
        """Linhas: (file_id, categoria, ambígua, ano, mês, caminho proposto, chave,
        pasta proposta, situação, ocorrências, regra, justificativa, estrutura)."""
        self._conn.executemany(
            """
            INSERT INTO organization_items (
                simulation_id, file_id, category, ambiguous, year, month, proposed_path,
                proposed_key, proposed_folder, status, issues, rule, justification,
                structure_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(self.simulation_id, *row) for row in rows],
        )

    def update_progress(self, processed: int) -> None:
        self._conn.execute(
            "UPDATE organization_simulations SET processed_count = ? WHERE id = ?",
            (processed, self.simulation_id),
        )

    # Conflitos e totais --------------------------------------------------

    def mark_conflicts(self) -> int:
        """Arquivos diferentes com o mesmo caminho proposto passam a 'conflict'.

        Nenhum é escolhido automaticamente; arquivos excluídos ou inválidos não
        participam da contagem e mantêm sua situação.
        """
        cursor = self._conn.execute(
            """
            UPDATE organization_items
            SET status = 'conflict',
                issues = CASE WHEN issues = '' THEN 'name_collision'
                              ELSE issues || ',name_collision' END
            WHERE simulation_id = ?
              AND status IN ('ok', 'review', 'already_in_place')
              AND proposed_key IN (
                  SELECT proposed_key FROM organization_items
                  WHERE simulation_id = ? AND proposed_key IS NOT NULL
                    AND status IN ('ok', 'review', 'already_in_place')
                  GROUP BY proposed_key HAVING COUNT(*) > 1
              )
            """,
            (self.simulation_id, self.simulation_id),
        )
        return cursor.rowcount

    def totals(self) -> dict:
        sid = self.simulation_id
        counts = dict(
            self._conn.execute(
                "SELECT status, COUNT(*) FROM organization_items WHERE simulation_id = ? "
                "GROUP BY status",
                (sid,),
            ).fetchall()
        )
        total_bytes, proposal_bytes = self._conn.execute(
            """
            SELECT COALESCE(SUM(f.size_bytes), 0),
                   COALESCE(SUM(CASE WHEN i.status != 'excluded' THEN f.size_bytes END), 0)
            FROM organization_items i JOIN indexed_files f ON f.id = i.file_id
            WHERE i.simulation_id = ?
            """,
            (sid,),
        ).fetchone()
        leaves = [
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT proposed_folder FROM organization_items "
                "WHERE simulation_id = ? AND proposed_folder IS NOT NULL",
                (sid,),
            )
        ]
        folders = set()
        for leaf in leaves:
            parts = leaf.replace("\\", "/").split("/")
            for depth in range(1, len(parts) + 1):
                folders.add("/".join(parts[:depth]))
        structures = self._conn.execute(
            "SELECT COUNT(*) FROM organization_structures WHERE simulation_id = ?", (sid,)
        ).fetchone()[0]
        return {
            "total_bytes": int(total_bytes),
            "proposal_bytes": int(proposal_bytes),
            "ok_count": int(counts.get("ok", 0)),
            "already_count": int(counts.get("already_in_place", 0)),
            "review_count": int(counts.get("review", 0)),
            "conflict_count": int(counts.get("conflict", 0)),
            "invalid_count": int(counts.get("invalid", 0)),
            "excluded_count": int(counts.get("excluded", 0)),
            "folders_count": len(folders),
            "structures_count": int(structures),
        }

    # Encerramento --------------------------------------------------------

    def finish(
        self,
        status: str,
        *,
        processed: int,
        totals: dict | None,
        elapsed_seconds: float,
        error_message: str | None,
    ) -> None:
        self.update_progress(processed)
        assignments = ""
        values: list = []
        if totals is not None:
            assignments = ", " + ", ".join(f"{name} = ?" for name in totals)
            values = list(totals.values())
        self._conn.execute(
            f"""
            UPDATE organization_simulations
            SET status = ?, finished_at = ?, elapsed_seconds = ?, error_message = ?{assignments}
            WHERE id = ?
            """,
            (status, utc_now(), round(elapsed_seconds, 3), error_message, *values, self.simulation_id),
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
