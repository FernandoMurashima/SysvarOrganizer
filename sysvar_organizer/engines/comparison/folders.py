"""Comparação inteligente de duas pastas indexadas (somente informativa).

Fluxo:

1. Valida as análises de origem (concluídas) e seleciona os arquivos de cada
   conjunto pela estrutura relativa à pasta escolhida.
2. Revalida cada arquivo físico (existência, tipo, tamanho, data, nuvem).
3. Calcula o SHA-256 somente dos tamanhos presentes nos dois lados,
   reaproveitando hashes confiáveis do cache conforme a política do Bloco 03.
   Caminhos do mesmo arquivo físico são lidos uma única vez.
4. Classifica por junções indexadas (caminho relativo; tamanho + SHA-256):
   idênticos, idênticos com nomes/locais diferentes, diferentes, exclusivos e
   indeterminados. A estrutura de pastas é comparada à parte.

Nenhum arquivo é alterado, copiado, movido ou excluído.
"""

from __future__ import annotations

import itertools
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
from sysvar_organizer.engines.comparison.models import (
    ComparisonOutcome,
    ComparisonProgress,
    ComparisonSet,
)
from sysvar_organizer.engines.file_analysis.locations import is_same_or_inside
from sysvar_organizer.engines.file_analysis.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from sysvar_organizer.persistence.comparison_store import ComparisonStore, Entry
from sysvar_organizer.persistence.duplicate_store import SourceAnalysisError

logger = logging.getLogger(__name__)

# Máximo de correspondências listadas por conteúdo (as demais são contadas).
PAIR_CAP_PER_CONTENT = 2000
BATCH = 2000

ProgressCallback = Callable[[ComparisonProgress], None]


def same_set(a: ComparisonSet, b: ComparisonSet) -> bool:
    return a.analysis_id == b.analysis_id and os.path.normcase(
        os.path.normpath(a.root)
    ) == os.path.normcase(os.path.normpath(b.root))


def sets_overlap(a: ComparisonSet, b: ComparisonSet) -> bool:
    """Indica se as árvores se sobrepõem fisicamente (uma contém a outra)."""
    return is_same_or_inside(a.root, b.root) or is_same_or_inside(b.root, a.root)


def relative_folder(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    return "" if rel == os.curdir else rel


class FolderComparer:
    def __init__(
        self,
        store: ComparisonStore,
        cancel_event: threading.Event,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 0.1,
        pair_cap: int = PAIR_CAP_PER_CONTENT,
    ) -> None:
        self._store = store
        self._cancel = cancel_event
        self._on_progress = on_progress
        self._interval = progress_interval
        self._pair_cap = pair_cap
        self._verifier = FileVerifier(store, cancel_event, self._emit)
        self._total = 0
        self._processed = 0
        self._files = {"A": 0, "B": 0}
        self._current_path = ""
        self._started = 0.0
        self._last_emit = 0.0

    # API -----------------------------------------------------------------

    def run(self, set_a: ComparisonSet, set_b: ComparisonSet) -> ComparisonOutcome:
        self._started = time.monotonic()
        if same_set(set_a, set_b):
            raise SourceAnalysisError("Os conjuntos A e B são o mesmo conjunto.")
        self._store.begin(set_a.analysis_id, set_a.root, set_b.analysis_id, set_b.root)
        logger.info("Comparação de pastas iniciada")

        status = STATUS_COMPLETED
        error_message = None
        totals = None
        try:
            self._materialize(set_a, set_b)
            self._emit(force=True)
            sizes_both = self._store.sizes_on_both_sides()
            for size, group in itertools.groupby(
                self._store.iter_entries_by_size(), key=lambda e: e.size
            ):
                if self._cancel.is_set():
                    break
                self._process_size(size, list(group), size in sizes_both)
                self._store.update_progress(
                    self._processed,
                    self._verifier.read_count,
                    self._verifier.reused,
                    self._verifier.bytes_read,
                )
                self._store.maybe_commit()
            if self._cancel.is_set():
                status = STATUS_CANCELLED
            else:
                totals = self._store.classify(self._pair_cap)
        except SourceAnalysisError as exc:
            status = STATUS_FAILED
            error_message = str(exc)
            self._discard()
        except Exception:  # noqa: BLE001 - falha inesperada encerra como 'failed'
            logger.exception("Falha inesperada na comparação de pastas")
            status = STATUS_FAILED
            error_message = "Falha inesperada durante a comparação. Detalhes no log do aplicativo."
            totals = None
            self._discard()

        elapsed = time.monotonic() - self._started
        try:
            self._store.finish(
                status,
                processed=self._processed,
                read_count=self._verifier.read_count,
                reused=self._verifier.reused,
                bytes_read=self._verifier.bytes_read,
                totals=totals,
                elapsed_seconds=elapsed,
                error_message=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao registrar o encerramento da comparação")
            status = STATUS_FAILED
            error_message = "Não foi possível gravar o resultado da comparação no banco local."

        self._current_path = ""
        self._emit(force=True)
        logger.info(
            "Comparação encerrada: id=%s estado=%s arquivos_a=%d arquivos_b=%d lidos=%d "
            "reaproveitados=%d bytes_lidos=%d tempo=%.1fs",
            self._store.comparison_id,
            status,
            self._files["A"],
            self._files["B"],
            self._verifier.read_count,
            self._verifier.reused,
            self._verifier.bytes_read,
            elapsed,
        )
        return ComparisonOutcome(
            comparison_id=self._store.comparison_id,
            status=status,
            files_a=self._files["A"],
            files_b=self._files["B"],
            processed=self._processed,
            read_count=self._verifier.read_count,
            reused=self._verifier.reused,
            bytes_read=self._verifier.bytes_read,
            elapsed_seconds=elapsed,
            error_message=error_message,
        )

    # Etapa 1: seleção dos conjuntos ------------------------------------

    def _materialize(self, set_a: ComparisonSet, set_b: ComparisonSet) -> None:
        folder_sets: dict[str, dict[str, str]] = {}
        for side, chosen in (("A", set_a), ("B", set_b)):
            folders: dict[int, str] = {}
            root_indexed = False
            for folder_id, path in self._store.analysis_folders(chosen.analysis_id):
                if is_same_or_inside(path, chosen.root):
                    rel = relative_folder(path, chosen.root)
                    folders[folder_id] = rel
                    root_indexed = root_indexed or rel == ""
            if not root_indexed:
                raise SourceAnalysisError(
                    f"A pasta do conjunto {side} não foi indexada na análise escolhida."
                )
            folder_sets[side] = {os.path.normcase(rel): rel for rel in folders.values() if rel}

            batch: list[tuple] = []
            for file_id, folder_id, name, size in self._store.iter_analysis_files(
                chosen.analysis_id
            ):
                rel_folder = folders.get(folder_id)
                if rel_folder is None:
                    continue
                rel_path = os.path.join(rel_folder, name) if rel_folder else name
                batch.append(
                    (
                        side,
                        file_id,
                        rel_path,
                        os.path.normcase(rel_path),
                        os.path.normcase(rel_folder),
                        os.path.normcase(name),
                        size,
                    )
                )
                self._files[side] += 1
                if len(batch) >= BATCH:
                    self._store.add_entries(batch)
                    batch.clear()
            if batch:
                self._store.add_entries(batch)

        keys_a, keys_b = folder_sets["A"], folder_sets["B"]
        rows = [(keys_a[k], "both") for k in keys_a.keys() & keys_b.keys()]
        rows += [(keys_a[k], "only_a") for k in keys_a.keys() - keys_b.keys()]
        rows += [(keys_b[k], "only_b") for k in keys_b.keys() - keys_a.keys()]
        self._store.add_folders(sorted(rows))
        self._store.set_file_counts(self._files["A"], self._files["B"])
        self._store.commit()
        self._total = self._files["A"] + self._files["B"]

    # Etapas 2 e 3: revalidação e SHA-256 --------------------------------

    def _process_size(self, size: int, entries: list[Entry], needs_content: bool) -> None:
        valid: list[tuple[Entry, os.stat_result]] = []
        for entry in entries:
            if self._cancel.is_set():
                return
            self._set_current(entry.full_path)
            try:
                st = self._verifier.validate(entry.full_path, size, entry.modified_at)
            except Unavailable as issue:
                self._store.mark_indeterminate(entry.id, issue.kind, issue.message)
                self._processed += 1
                continue
            valid.append((entry, st))

        sides = {entry.side for entry, _ in valid}
        if not needs_content or sides != {"A", "B"}:
            # Sem arquivo válido do mesmo tamanho do outro lado: o conteúdo não
            # pode coincidir, então não há o que ler.
            for entry, st in valid:
                self._store.mark_ok(entry.id, None, False, physical_key(st, entry.id))
                self._processed += 1
            return

        by_physical: dict[str, list[tuple[Entry, os.stat_result]]] = {}
        for entry, st in valid:
            by_physical.setdefault(physical_key(st, entry.id), []).append((entry, st))

        for key, members in by_physical.items():
            if self._cancel.is_set():
                return
            first, first_stat = members[0]
            self._set_current(first.full_path)
            try:
                digest, reused = self._verifier.sha256(first.full_path, first_stat, size)
            except Cancelled:
                return
            except Unavailable as issue:
                for entry, _ in members:
                    self._store.mark_indeterminate(entry.id, issue.kind, issue.message)
                    self._processed += 1
                continue
            for entry, _ in members:
                self._store.mark_ok(entry.id, digest, reused, key)
                self._processed += 1
            self._emit()

    # Progresso -----------------------------------------------------------

    def _discard(self) -> None:
        try:
            self._store.discard_pending()
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao descartar alterações pendentes")

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
            ComparisonProgress(
                total=self._total,
                processed=self._processed,
                read_count=self._verifier.read_count,
                reused=self._verifier.reused,
                bytes_read=self._verifier.bytes_read,
                current_path=self._current_path,
                elapsed_seconds=now - self._started,
            )
        )


# Resumo de um conjunto para a tela de seleção --------------------------------


class SetSummary:
    def __init__(self, indexed: bool, files: int, total_bytes: int) -> None:
        self.indexed = indexed
        self.files = files
        self.total_bytes = total_bytes


def summarize_set(repository, analysis_id: int, root: str) -> SetSummary:
    """Arquivos e volume indexados sob ``root`` na análise informada.

    ``repository`` é um ComparisonRepository (somente leitura).
    """
    totals = repository.file_totals_by_folder(analysis_id)
    indexed = False
    files = 0
    total_bytes = 0
    for folder_id, path in repository.folder_paths(analysis_id):
        if is_same_or_inside(path, root):
            indexed = indexed or relative_folder(path, root) == ""
            count, size = totals.get(folder_id, (0, 0))
            files += count
            total_bytes += size
    return SetSummary(indexed, files, total_bytes)
