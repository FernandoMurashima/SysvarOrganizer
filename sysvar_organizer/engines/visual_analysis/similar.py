"""Busca de fotografias semelhantes sobre uma análise de armazenamento.

Fluxo:

1. Seleciona as imagens candidatas (JPEG, PNG, WEBP, BMP, TIFF) indexadas sob
   a pasta escolhida da análise concluída.
2. Revalida cada arquivo (existência, tipo, tamanho, data, link, nuvem) e
   reaproveita a assinatura do cache somente se identidade, metadados e versão
   do algoritmo coincidirem; caso contrário calcula pHash e dHash.
3. Busca pares por índice multi-bloco, classifica por faixas e forma grupos
   por ligação completa (sem encadeamento).
4. Dentro de cada grupo, confirma cópias binariamente idênticas por SHA-256
   (mesmo tamanho), reaproveitando hashes revalidados.

Somente leitura. Resultados só são confirmados se a busca terminar sem
cancelamento nem falha.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from itertools import combinations

from sysvar_organizer.engines.comparison.folders import relative_folder
from sysvar_organizer.engines.comparison.hashing import (
    Cancelled,
    FileVerifier,
    Unavailable,
    os_issue,
    path_key,
    physical_key,
)
from sysvar_organizer.engines.file_analysis.locations import is_same_or_inside
from sysvar_organizer.engines.file_analysis.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from sysvar_organizer.engines.visual_analysis.grouping import (
    BAND_EXACT,
    BAND_PROBABLE,
    BAND_UNCERTAIN,
    BAND_VERY_HIGH,
    ImageSig,
    build_groups,
    criteria_description,
    similar_pairs,
)
from sysvar_organizer.engines.visual_analysis.models import (
    PHASE_EXACT,
    PHASE_GROUPING,
    PHASE_SIGNATURES,
    VisualOutcome,
    VisualProgress,
)
from sysvar_organizer.engines.visual_analysis.signatures import (
    IMAGE_EXTENSIONS,
    SIGNATURE_VERSION,
    ImageRejected,
    compute_signature,
    dependencies_error,
    from_hex,
    to_hex,
)
from sysvar_organizer.persistence.visual_store import (
    CachedSignature,
    SourceAnalysisError,
    VisualStore,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[VisualProgress], None]


class SimilarityFinder:
    def __init__(
        self,
        store: VisualStore,
        cancel_event: threading.Event,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 0.1,
    ) -> None:
        self._store = store
        self._cancel = cancel_event
        self._on_progress = on_progress
        self._interval = progress_interval
        self._verifier = FileVerifier(store, cancel_event, self._emit)
        self._phase = PHASE_SIGNATURES
        self._candidates = 0
        self._processed = 0
        self._computed = 0
        self._reused = 0
        self._skipped = 0
        self._current_path = ""
        self._started = 0.0
        self._last_emit = 0.0

    # API -----------------------------------------------------------------

    def run(self, analysis_id: int, root: str) -> VisualOutcome:
        self._started = time.monotonic()
        missing = dependencies_error()
        if missing:
            raise SourceAnalysisError(missing)
        self._store.begin(analysis_id, root, f"{SIGNATURE_VERSION}; {criteria_description()}")
        logger.info("Busca de fotografias semelhantes iniciada")

        status = STATUS_COMPLETED
        error_message = None
        totals = None
        try:
            self._materialize(analysis_id, root)
            self._emit(force=True)
            for entry in self._store.iter_pending():
                if self._cancel.is_set():
                    break
                self._process(entry)
                self._store.maybe_commit()
            self._store.update_progress(
                self._processed, self._computed, self._reused, self._skipped
            )
            self._store.commit()
            if not self._cancel.is_set():
                totals = self._group()
            if self._cancel.is_set():
                status = STATUS_CANCELLED
                totals = None
                self._discard()  # grupos parciais nunca são confirmados
        except SourceAnalysisError as exc:
            status = STATUS_FAILED
            error_message = str(exc)
            totals = None
            self._discard()
        except Exception:  # noqa: BLE001 - falha inesperada encerra como 'failed'
            logger.exception("Falha inesperada na busca de fotografias semelhantes")
            status = STATUS_FAILED
            error_message = "Falha inesperada durante a busca. Detalhes no log do aplicativo."
            totals = None
            self._discard()

        elapsed = time.monotonic() - self._started
        try:
            self._store.finish(
                status,
                processed=self._processed,
                computed=self._computed,
                reused=self._reused,
                skipped=self._skipped,
                totals=totals,
                elapsed_seconds=elapsed,
                error_message=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao registrar o encerramento da busca visual")
            status = STATUS_FAILED
            totals = None
            error_message = "Não foi possível gravar o resultado da busca no banco local."

        self._current_path = ""
        self._emit(force=True)
        logger.info(
            "Busca visual encerrada: estado=%s candidatas=%d calculadas=%d reaproveitadas=%d "
            "ignoradas=%d grupos=%s tempo=%.1fs",
            status,
            self._candidates,
            self._computed,
            self._reused,
            self._skipped,
            totals["groups_count"] if totals else "-",
            elapsed,
        )
        return VisualOutcome(
            search_id=self._store.search_id,
            status=status,
            candidates=self._candidates,
            processed=self._processed,
            computed=self._computed,
            reused=self._reused,
            skipped=self._skipped,
            groups=totals["groups_count"] if totals else None,
            grouped_images=totals["grouped_images"] if totals else None,
            elapsed_seconds=elapsed,
            error_message=error_message,
        )

    # 1. Candidatas -------------------------------------------------------

    def _materialize(self, analysis_id: int, root: str) -> None:
        folders = set()
        root_indexed = False
        for folder_id, path in self._store.analysis_folders(analysis_id):
            if is_same_or_inside(path, root):
                folders.add(folder_id)
                root_indexed = root_indexed or relative_folder(path, root) == ""
        if not root_indexed:
            raise SourceAnalysisError("A pasta escolhida não foi indexada na análise.")
        batch: list[int] = []
        for file_id, folder_id in self._store.iter_image_files(analysis_id, IMAGE_EXTENSIONS):
            if folder_id in folders:
                batch.append(file_id)
                self._candidates += 1
                if len(batch) >= 1000:
                    self._store.add_candidates(batch)
                    batch.clear()
        if batch:
            self._store.add_candidates(batch)
        self._store.set_candidates(self._candidates)
        self._store.commit()

    # 2. Assinaturas ------------------------------------------------------

    def _process(self, entry) -> None:
        self._set_current(entry.full_path)
        try:
            st = self._verifier.validate(entry.full_path, entry.size, entry.modified_at)
        except Unavailable as issue:
            self._skip(entry.id, issue.kind, issue.message)
            return

        key = path_key(entry.full_path)
        volume_id, file_index = str(st.st_dev), str(st.st_ino)
        cached = None
        if st.st_ino:
            cached = self._store.cached_signature(
                key, entry.size, st.st_mtime_ns, volume_id, file_index, SIGNATURE_VERSION
            )
        if cached is not None:
            self._reused += 1
            signature, reused = cached, True
        else:
            try:
                computed = compute_signature(entry.full_path, (st.st_size, st.st_mtime_ns))
            except ImageRejected as rejected:
                self._skip(entry.id, rejected.kind, rejected.message)
                return
            except OSError as exc:
                issue = os_issue(exc)
                self._skip(entry.id, issue.kind, issue.message)
                return
            signature = CachedSignature(
                to_hex(computed.phash),
                to_hex(computed.dhash),
                computed.width,
                computed.height,
                computed.format,
            )
            reused = False
            self._computed += 1
            if st.st_ino:
                self._store.save_signature(
                    key, entry.size, st.st_mtime_ns, volume_id, file_index,
                    SIGNATURE_VERSION, signature,
                )
        self._store.mark_ok(entry.id, signature, reused, physical_key(st, entry.id))
        self._processed += 1

    # 3 e 4. Agrupamento e cópias exatas ---------------------------------

    def _group(self) -> dict | None:
        self._phase = PHASE_GROUPING
        self._current_path = ""
        self._emit(force=True)
        images = self._store.ok_images()
        sigs = [
            ImageSig(i, from_hex(img.phash), from_hex(img.dhash), img.width, img.height)
            for i, img in enumerate(images)
        ]
        pairs = similar_pairs(sigs)
        groups = build_groups(sigs, pairs)

        self._phase = PHASE_EXACT
        counts = {BAND_EXACT: 0, BAND_VERY_HIGH: 0, BAND_PROBABLE: 0, BAND_UNCERTAIN: 0}
        grouped_images = 0
        for group in groups:
            if self._cancel.is_set():
                return None
            members = group.members
            # Referência: a imagem de maior resolução (provável original).
            rep = max(members, key=lambda m: (sigs[m].width * sigs[m].height, -m))
            ordered = [rep] + sorted(m for m in members if m != rep)
            hashes = self._exact_hashes(images, ordered)
            if hashes is None:
                return None

            physical_counts = defaultdict(set)
            for m in ordered:
                if hashes.get(m) is not None:
                    physical_counts[hashes[m]].add(images[m].physical_key)
            exact = {
                m for m in ordered
                if hashes.get(m) is not None and len(physical_counts[hashes[m]]) > 1
            }
            same_physical_keys = defaultdict(int)
            for m in ordered:
                same_physical_keys[images[m].physical_key] += 1

            distinct_physical = {images[m].physical_key for m in ordered}
            band = group.band
            all_hashes = {hashes.get(m) for m in ordered}
            if len(all_hashes) == 1 and None not in all_hashes and len(distinct_physical) > 1:
                band = BAND_EXACT  # todos binariamente idênticos

            max_p = max_d = 0
            for a, b in combinations(ordered, 2):
                max_p = max(max_p, (sigs[a].phash ^ sigs[b].phash).bit_count())
                max_d = max(max_d, (sigs[a].dhash ^ sigs[b].dhash).bit_count())

            group_id = self._store.add_group(
                band, len(ordered), max_p, max_d, len(exact), images[rep].id
            )
            rows = []
            for position, m in enumerate(ordered):
                rows.append(
                    (
                        group_id,
                        images[m].id,
                        position,
                        (sigs[m].phash ^ sigs[rep].phash).bit_count(),
                        (sigs[m].dhash ^ sigs[rep].dhash).bit_count(),
                        hashes.get(m),
                        int(m in exact),
                        int(same_physical_keys[images[m].physical_key] > 1),
                    )
                )
            self._store.add_members(rows)
            counts[band] += 1
            grouped_images += len(ordered)
        return {
            "pairs_count": len(pairs),
            "groups_count": len(groups),
            "grouped_images": grouped_images,
            "exact_groups": counts[BAND_EXACT],
            "very_high_groups": counts[BAND_VERY_HIGH],
            "probable_groups": counts[BAND_PROBABLE],
            "uncertain_groups": counts[BAND_UNCERTAIN],
        }

    def _exact_hashes(self, images, indices) -> dict[int, str | None] | None:
        """SHA-256 dos integrantes que têm outro integrante com o mesmo tamanho.

        Retorna {índice -> sha256 ou None}; None se a busca for cancelada.
        Arquivos que não puderem ser revalidados ficam sem confirmação binária.
        """
        by_size = defaultdict(list)
        for index in indices:
            by_size[images[index].size].append(index)
        hashes: dict[int, str | None] = {}
        for size, same_size in by_size.items():
            if len(same_size) < 2:
                continue
            for index in same_size:
                image = images[index]
                self._set_current(image.full_path)
                try:
                    st = self._verifier.validate(image.full_path, size, image.modified_at)
                    hashes[index], _ = self._verifier.sha256(image.full_path, st, size)
                except Cancelled:
                    return None
                except Unavailable:
                    hashes[index] = None
        return hashes

    # Auxiliares ----------------------------------------------------------

    def _skip(self, image_id: int, kind: str, message: str) -> None:
        self._store.mark_skipped(image_id, kind, message)
        self._skipped += 1
        self._processed += 1

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
            VisualProgress(
                phase=self._phase,
                candidates=self._candidates,
                processed=self._processed,
                computed=self._computed,
                reused=self._reused,
                skipped=self._skipped,
                bytes_read=self._verifier.bytes_read,
                current_path=self._current_path,
                elapsed_seconds=now - self._started,
            )
        )


def estimate_candidates(repository, analysis_id: int, root: str) -> tuple[int, int, bool]:
    """(imagens candidatas, bytes, pasta indexada) sob ``root``. Somente leitura."""
    totals = repository.image_totals_by_folder(analysis_id, IMAGE_EXTENSIONS)
    count = size = 0
    indexed = False
    for folder_id, path in repository.folder_paths(analysis_id):
        if is_same_or_inside(path, root):
            indexed = indexed or relative_folder(path, root) == ""
            c, s = totals.get(folder_id, (0, 0))
            count += c
            size += s
    return count, size, indexed
