"""Identificação de duplicados exatos por SHA-256.

Etapas:

A. Candidatos: arquivos da análise escolhida cujo tamanho se repete.
B. Para cada tamanho, valida existência, tipo, tamanho e data de modificação
   de cada arquivo e calcula o SHA-256 em blocos, apenas quando restam ao
   menos dois arquivos físicos distintos. Um hash anterior só é reaproveitado
   se tamanho, data de modificação (ns), volume e identificador do arquivo
   atuais coincidirem com os registrados.
C. Agrupamento por tamanho e SHA-256, somente se a busca terminar sem
   cancelamento nem falha.

Segurança: os arquivos são abertos apenas para leitura. Links simbólicos,
junções, pontos de montagem e arquivos disponíveis somente na nuvem não são
lidos. Arquivos alterados desde a análise ou durante a leitura não entram em
grupos. Caminhos que levam ao mesmo arquivo físico (hard links) são lidos uma
única vez e não contam como cópias excedentes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from sysvar_organizer.engines.comparison.hashing import (
    Cancelled,
    FileVerifier,
    Unavailable,
    physical_key,
)
from sysvar_organizer.engines.comparison.models import DuplicateProgress, DuplicateResult
from sysvar_organizer.engines.file_analysis.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from sysvar_organizer.persistence.duplicate_store import (
    Candidate,
    DuplicateStore,
    GroupTotals,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[DuplicateProgress], None]


class DuplicateFinder:
    def __init__(
        self,
        store: DuplicateStore,
        cancel_event: threading.Event,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 0.1,
    ) -> None:
        self._store = store
        self._cancel = cancel_event
        self._on_progress = on_progress
        self._interval = progress_interval
        self._candidates = 0
        self._processed = 0
        self._skipped = 0
        self._verifier = FileVerifier(store, cancel_event, self._emit)
        self._current_path = ""
        self._started = 0.0
        self._last_emit = 0.0

    # Contadores do cálculo de SHA-256 (mantidos pelo verificador compartilhado)
    @property
    def _read_count(self) -> int:
        return self._verifier.read_count

    @property
    def _reused(self) -> int:
        return self._verifier.reused

    @property
    def _bytes_read(self) -> int:
        return self._verifier.bytes_read

    # API -----------------------------------------------------------------

    def run(self, analysis_id: int) -> DuplicateResult:
        self._started = time.monotonic()
        self._candidates = self._store.begin(analysis_id)
        logger.info("Busca de duplicados iniciada: %d candidato(s)", self._candidates)
        self._emit(force=True)

        status = STATUS_COMPLETED
        error_message = None
        totals: GroupTotals | None = None
        try:
            for size in self._store.candidate_sizes():
                if self._cancel.is_set():
                    break
                self._process_size(size)
                self._store.update_progress(
                    self._processed, self._read_count, self._reused, self._bytes_read, self._skipped
                )
                self._store.maybe_commit()
            if self._cancel.is_set():
                status = STATUS_CANCELLED
            else:
                totals = self._store.build_groups()
        except Exception:  # noqa: BLE001 - falha inesperada encerra a busca como 'failed'
            logger.exception("Falha inesperada na busca de duplicados")
            status = STATUS_FAILED
            error_message = "Falha inesperada durante a busca. Detalhes no log do aplicativo."
            totals = None
            try:
                self._store.discard_pending()
            except Exception:  # noqa: BLE001
                logger.exception("Falha ao descartar alterações pendentes")

        elapsed = time.monotonic() - self._started
        try:
            self._store.finish(
                status,
                processed=self._processed,
                read_count=self._read_count,
                reused=self._reused,
                bytes_read=self._bytes_read,
                skipped=self._skipped,
                totals=totals,
                elapsed_seconds=elapsed,
                error_message=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao registrar o encerramento da busca")
            status = STATUS_FAILED
            totals = None
            error_message = "Não foi possível gravar o resultado da busca no banco local."

        self._current_path = ""
        self._emit(force=True)
        logger.info(
            "Busca de duplicados encerrada: estado=%s candidatos=%d lidos=%d reaproveitados=%d "
            "ignorados=%d bytes_lidos=%d grupos=%s tempo=%.1fs",
            status,
            self._candidates,
            self._read_count,
            self._reused,
            self._skipped,
            self._bytes_read,
            totals.groups if totals else "-",
            elapsed,
        )
        return DuplicateResult(
            search_id=self._store.search_id,
            status=status,
            candidates=self._candidates,
            processed=self._processed,
            read_count=self._read_count,
            reused=self._reused,
            skipped=self._skipped,
            bytes_read=self._bytes_read,
            groups=totals.groups if totals else None,
            group_files=totals.group_files if totals else None,
            logical_bytes=totals.logical_bytes if totals else None,
            excess_copies=totals.excess_copies if totals else None,
            recoverable_bytes=totals.recoverable_bytes if totals else None,
            hardlink_aliases=totals.hardlink_aliases if totals else None,
            elapsed_seconds=elapsed,
            error_message=error_message,
        )

    # Processamento por tamanho -----------------------------------------

    def _process_size(self, size: int) -> None:
        valid: list[tuple[Candidate, os.stat_result]] = []
        for candidate in self._store.candidates_of_size(size):
            if self._cancel.is_set():
                return
            full_path = os.path.join(candidate.folder, candidate.name)
            self._set_current(full_path)
            try:
                st = self._verifier.validate(full_path, size, candidate.modified_at)
            except Unavailable as issue:
                self._skip(candidate, full_path, issue)
                continue
            valid.append((candidate, st))

        by_physical: dict[str, list[tuple[Candidate, os.stat_result]]] = {}
        for candidate, st in valid:
            by_physical.setdefault(physical_key(st, candidate.row_id), []).append((candidate, st))

        if len(by_physical) < 2:
            # Restou no máximo um arquivo físico com este tamanho: não há o que comparar.
            for candidate, _ in valid:
                self._store.mark_not_needed(candidate.row_id)
                self._processed += 1
            return

        for key, members in by_physical.items():
            if self._cancel.is_set():
                return
            first, first_stat = members[0]
            first_path = os.path.join(first.folder, first.name)
            self._set_current(first_path)
            try:
                sha256, reused = self._verifier.sha256(first_path, first_stat, size)
            except Cancelled:
                return
            except Unavailable as issue:
                for candidate, _ in members:
                    self._skip(candidate, os.path.join(candidate.folder, candidate.name), issue)
                continue
            for candidate, _ in members:
                self._store.mark_hashed(candidate.row_id, sha256, reused, key)
                self._processed += 1
            self._emit()

    # Contadores e progresso ---------------------------------------------

    def _skip(self, candidate: Candidate, full_path: str, issue: Unavailable) -> None:
        self._store.mark_skipped(
            candidate.row_id, candidate.file_id, issue.kind, full_path, issue.message
        )
        self._skipped += 1
        self._processed += 1

    def _set_current(self, path: str) -> None:
        self._current_path = path
        self._emit()

    def _emit(self, force: bool = False) -> None:
        if self._on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < self._interval:
            return
        self._last_emit = now
        self._on_progress(
            DuplicateProgress(
                candidates=self._candidates,
                processed=self._processed,
                read_count=self._read_count,
                reused=self._reused,
                skipped=self._skipped,
                bytes_read=self._bytes_read,
                current_path=self._current_path,
                elapsed_seconds=now - self._started,
            )
        )
