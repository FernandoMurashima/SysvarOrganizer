"""Simulação de organização sobre os metadados de uma análise concluída.

Para cada arquivo do conjunto: categoria (tipo aparente pela extensão), ano e
mês (data de modificação indexada, quando o modelo usa data), caminho
proposto, regra aplicada e situação. Depois, arquivos diferentes que
receberiam o mesmo caminho passam a "requer decisão", sem escolha automática.

Nenhum arquivo é aberto, e nada é criado, copiado, movido ou renomeado no
disco, nem no destino simulado. Apenas registros no banco local são gravados.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable

from sysvar_organizer.engines.comparison.folders import relative_folder
from sysvar_organizer.engines.file_analysis.locations import is_same_or_inside
from sysvar_organizer.engines.file_analysis.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from sysvar_organizer.engines.organization import sensitive
from sysvar_organizer.engines.organization.categories import classify
from sysvar_organizer.engines.organization.models import (
    ITEM_ALREADY,
    ITEM_EXCLUDED,
    ITEM_INVALID,
    ITEM_OK,
    ITEM_REVIEW,
    OrganizationOutcome,
    OrganizationProgress,
    OrganizationRequest,
)
from sysvar_organizer.engines.organization.rules import (
    DATE_SOURCE,
    MAX_PATH,
    MODELS,
    date_info,
    destination_alerts,
    folder_parts,
    rule_text,
    windows_name_issue,
)
from sysvar_organizer.persistence.organization_store import (
    OrganizationStore,
    SourceAnalysisError,
)

logger = logging.getLogger(__name__)

BATCH = 1000

ProgressCallback = Callable[[OrganizationProgress], None]


class OrganizationPlanner:
    def __init__(
        self,
        store: OrganizationStore,
        cancel_event: threading.Event,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 0.1,
    ) -> None:
        self._store = store
        self._cancel = cancel_event
        self._on_progress = on_progress
        self._interval = progress_interval
        self._total = 0
        self._processed = 0
        self._current_path = ""
        self._started = 0.0
        self._last_emit = 0.0

    def run(self, request: OrganizationRequest) -> OrganizationOutcome:
        self._started = time.monotonic()
        model = MODELS.get(request.model)
        if model is None:
            raise SourceAnalysisError("Modelo de organização desconhecido.")
        alerts, usable = destination_alerts(request.root, request.destination)
        if not usable:
            raise SourceAnalysisError(alerts[-1] if alerts else "Destino inválido.")
        self._store.begin(
            request.analysis_id,
            request.root,
            request.model,
            request.destination,
            request.exclude_sensitive,
            rule_text(model),
            json.dumps(alerts, ensure_ascii=False),
        )
        logger.info("Simulação de organização iniciada (modelo %s)", request.model)

        status = STATUS_COMPLETED
        error_message = None
        totals = None
        try:
            folders = self._set_folders(request)
            structures = self._detect_structures(request, folders)
            self._emit(force=True)
            self._classify_all(request, model, folders, structures)
            if self._cancel.is_set():
                status = STATUS_CANCELLED
            else:
                self._store.mark_conflicts()
                totals = self._store.totals()
        except SourceAnalysisError as exc:
            status = STATUS_FAILED
            error_message = str(exc)
            self._discard()
        except Exception:  # noqa: BLE001 - falha inesperada encerra como 'failed'
            logger.exception("Falha inesperada na simulação de organização")
            status = STATUS_FAILED
            error_message = "Falha inesperada durante a simulação. Detalhes no log do aplicativo."
            self._discard()

        elapsed = time.monotonic() - self._started
        try:
            self._store.finish(
                status,
                processed=self._processed,
                totals=totals,
                elapsed_seconds=elapsed,
                error_message=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao registrar o encerramento da simulação")
            status = STATUS_FAILED
            error_message = "Não foi possível gravar o resultado da simulação no banco local."

        self._current_path = ""
        self._emit(force=True)
        logger.info(
            "Simulação encerrada: estado=%s arquivos=%d tempo=%.1fs",
            status,
            self._processed,
            elapsed,
        )
        return OrganizationOutcome(
            simulation_id=self._store.simulation_id,
            status=status,
            total=self._total,
            processed=self._processed,
            elapsed_seconds=elapsed,
            error_message=error_message,
        )

    # Conjunto de origem --------------------------------------------------

    def _set_folders(self, request: OrganizationRequest) -> dict[int, str]:
        folders: dict[int, str] = {}
        root_indexed = False
        for folder_id, path in self._store.analysis_folders(request.analysis_id):
            if is_same_or_inside(path, request.root):
                folders[folder_id] = path
                root_indexed = root_indexed or relative_folder(path, request.root) == ""
        if not root_indexed:
            raise SourceAnalysisError("A pasta de origem não foi indexada na análise escolhida.")
        return folders

    def _detect_structures(
        self, request: OrganizationRequest, folders: dict[int, str]
    ) -> dict[str, sensitive.Structure]:
        names = []
        for _file_id, folder_id, name, _ext, _size, _mod in self._store.iter_analysis_files(
            request.analysis_id
        ):
            path = folders.get(folder_id)
            if path is not None:
                names.append((path, name))
                self._total += 1
            if self._cancel.is_set():
                break
        found = sensitive.detect(folders.values(), names, request.root)
        counts: dict[str, int] = {}
        for path, _name in names:
            structure = sensitive.find_protecting(path, found)
            if structure is not None:
                counts[structure.path] = counts.get(structure.path, 0) + 1
        self._store.add_structures(
            [(s.path, s.kind, s.reason, counts.get(s.path, 0)) for s in found.values()]
        )
        self._store.set_files_count(self._total)
        self._store.commit()
        return found

    # Classificação e proposta --------------------------------------------

    def _classify_all(self, request, model, folders, structures) -> None:
        destination = request.destination
        batch: list[tuple] = []
        for file_id, folder_id, name, extension, _size, modified_at in self._store.iter_analysis_files(
            request.analysis_id
        ):
            if self._cancel.is_set():
                return
            folder = folders.get(folder_id)
            if folder is None:
                continue
            current = os.path.join(folder, name)
            self._set_current(current)
            batch.append(
                self._plan(file_id, folder, name, extension, modified_at, current, model,
                           destination, structures, request.exclude_sensitive)
            )
            self._processed += 1
            if len(batch) >= BATCH:
                self._store.add_items(batch)
                batch.clear()
                self._store.update_progress(self._processed)
                self._store.maybe_commit()
        if batch:
            self._store.add_items(batch)
        self._store.update_progress(self._processed)

    def _plan(
        self, file_id, folder, name, extension, modified_at, current, model,
        destination, structures, exclude_sensitive,
    ) -> tuple:
        result = classify(extension or "")
        issues: list[str] = []
        if result.ambiguous:
            issues.append("ambiguous_extension")
        if result.unknown_extension:
            issues.append("unknown_extension")
        if result.category == "no_extension":
            issues.append("no_extension")

        date = date_info(modified_at)
        if model.use_year and date.issue:
            issues.append(date.issue)
        year = date.year if model.use_year else None
        month = date.month if model.use_month else None

        structure = sensitive.find_protecting(folder, structures)
        justification = result.justification
        if structure is not None:
            issues.append("sensitive_structure")
            justification += (
                f" Pertence a uma estrutura sensível ({sensitive.KIND_LABELS[structure.kind]}: "
                f"{structure.path}). {structure.reason} Não deve ser movido individualmente."
            )

        parts = folder_parts(model, result.category, date)
        rule = f"Modelo {model.key}: {' / '.join(parts)}"
        if model.use_year:
            rule += f" ({DATE_SOURCE})"

        if structure is not None and exclude_sensitive:
            return (
                file_id, result.category, int(result.ambiguous), year, month, None, None, None,
                ITEM_EXCLUDED, ",".join(issues), rule, justification, structure.path,
            )

        proposed_folder = os.path.join(*parts)
        proposed = os.path.join(destination, proposed_folder, name)
        name_issue = windows_name_issue(name)
        if name_issue:
            issues.append("invalid_name")
            justification += f" {name_issue}"
        if len(proposed) > MAX_PATH:
            issues.append("long_path")

        if name_issue or len(proposed) > MAX_PATH:
            status = ITEM_INVALID
        elif issues:
            status = ITEM_REVIEW
        elif os.path.normcase(os.path.normpath(current)) == os.path.normcase(
            os.path.normpath(proposed)
        ):
            status = ITEM_ALREADY
            issues.append("already_in_place")
        else:
            status = ITEM_OK
        return (
            file_id,
            result.category,
            int(result.ambiguous),
            year,
            month,
            proposed,
            os.path.normcase(os.path.normpath(proposed)),
            proposed_folder,
            status,
            ",".join(issues),
            rule,
            justification,
            structure.path if structure else None,
        )

    # Auxiliares ----------------------------------------------------------

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
            OrganizationProgress(
                total=self._total,
                processed=self._processed,
                current_path=self._current_path,
                elapsed_seconds=now - self._started,
            )
        )
