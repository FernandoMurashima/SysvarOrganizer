"""Varredura recursiva somente leitura.

Regras de segurança:

* Somente leitura de metadados de diretório (``os.scandir`` e ``stat`` sem
  seguir links). Nenhum arquivo é aberto, alterado, movido ou excluído.
* Links simbólicos, junções e pontos de montagem não são seguidos; são
  contados como ignorados e registrados. Isso também impede ciclos.
* Falhas de acesso em arquivos ou pastas isoladas são registradas e a
  varredura continua.
* Percurso iterativo, com pilha de pastas pendentes; os arquivos são
  gravados em lotes, sem manter a lista completa em memória.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sysvar_organizer.engines.file_analysis.locations import is_volume_root
from sysvar_organizer.engines.file_analysis.models import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    ScanLocation,
    ScanProgress,
    ScanResult,
)
from sysvar_organizer.persistence.scan_store import ScanStore

logger = logging.getLogger(__name__)

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # junções e pontos de montagem
IO_REPARSE_TAG_SYMLINK = 0xA000000C

# Pastas de sistema na raiz de cada unidade que não são indexadas.
SYSTEM_ROOT_FOLDERS = frozenset({"$recycle.bin", "system volume information"})

ISSUE_ACCESS_DENIED = "access_denied"
ISSUE_NOT_FOUND = "not_found"
ISSUE_READ_ERROR = "read_error"
ISSUE_NOT_A_FOLDER = "not_a_folder"
ISSUE_SYMLINK = "symlink_not_followed"
ISSUE_JUNCTION = "junction_not_followed"
ISSUE_SYSTEM_FOLDER = "system_folder_skipped"
ISSUE_SPECIAL_ENTRY = "special_entry_skipped"

LOCATION_COMPLETED = "completed"
LOCATION_CANCELLED = "cancelled"
LOCATION_INACCESSIBLE = "inaccessible"

ProgressCallback = Callable[[ScanProgress], None]


@dataclass
class _Counters:
    files: int = 0
    folders: int = 0
    total_bytes: int = 0
    inaccessible: int = 0
    skipped: int = 0


def to_filesystem_path(path: str) -> str:
    """No Windows, usa o prefixo de caminho estendido para suportar caminhos longos."""
    if sys.platform != "win32":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _issue_kind(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return ISSUE_ACCESS_DENIED
    if isinstance(exc, FileNotFoundError):
        return ISSUE_NOT_FOUND
    return ISSUE_READ_ERROR


def _issue_message(exc: OSError) -> str:
    detail = exc.strerror or str(exc.errno or "")
    return f"{exc.__class__.__name__}: {detail}".strip()


def _reparse_link_kind(st: os.stat_result) -> str | None:
    tag = getattr(st, "st_reparse_tag", 0)
    if tag == IO_REPARSE_TAG_SYMLINK:
        return ISSUE_SYMLINK
    if tag == IO_REPARSE_TAG_MOUNT_POINT:
        return ISSUE_JUNCTION
    return None


def _entry_link_kind(entry: os.DirEntry) -> str | None:
    if entry.is_symlink():
        return ISSUE_SYMLINK
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    if is_junction is not None:
        if is_junction():
            return ISSUE_JUNCTION
    elif sys.platform == "win32":
        return _reparse_link_kind(entry.stat(follow_symlinks=False))
    return None


def _creation_time(st: os.stat_result) -> int | None:
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return int(birth)
    if sys.platform == "win32":
        return int(st.st_ctime)  # no Windows, st_ctime é a data de criação
    return None


class StorageScanner:
    def __init__(
        self,
        store: ScanStore,
        cancel_event: threading.Event,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 0.1,
    ) -> None:
        self._store = store
        self._cancel = cancel_event
        self._on_progress = on_progress
        self._interval = progress_interval
        self._total = _Counters()
        self._loc = _Counters()
        self._current_path = ""
        self._started = 0.0
        self._last_emit = 0.0

    # API -----------------------------------------------------------------

    def run(self, locations: Sequence[ScanLocation]) -> ScanResult:
        self._started = time.monotonic()
        summary = locations[0].path if len(locations) == 1 else f"{len(locations)} locais"
        location_ids = self._store.begin([(loc.path, loc.kind) for loc in locations], summary)
        logger.info("Varredura iniciada: %d local(is)", len(locations))

        status = STATUS_COMPLETED
        error_message = None
        try:
            for location, location_id in zip(locations, location_ids):
                if self._cancel.is_set():
                    break
                self._loc = _Counters()
                location_status = self._scan_location(location, location_id)
                self._store.finish_location(
                    location_id,
                    location_status,
                    self._loc.files,
                    self._loc.folders,
                    self._loc.total_bytes,
                )
            if self._cancel.is_set():
                status = STATUS_CANCELLED
        except Exception:  # noqa: BLE001 - falha inesperada encerra a análise como 'failed'
            logger.exception("Falha inesperada durante a varredura")
            status = STATUS_FAILED
            error_message = "Falha inesperada durante a análise. Detalhes no log do aplicativo."
            try:
                self._store.discard_pending()
            except Exception:  # noqa: BLE001
                logger.exception("Falha ao descartar o lote pendente")

        elapsed = time.monotonic() - self._started
        total = self._total
        has_warnings = total.inaccessible > 0 or total.skipped > 0
        try:
            self._store.finish(
                status,
                files=total.files,
                folders=total.folders,
                total_bytes=total.total_bytes,
                inaccessible=total.inaccessible,
                skipped=total.skipped,
                has_warnings=has_warnings,
                elapsed_seconds=elapsed,
                error_message=error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao registrar o encerramento da análise")
            status = STATUS_FAILED
            error_message = "Não foi possível gravar o resultado da análise no banco local."

        self._emit(force=True)
        logger.info(
            "Varredura encerrada: estado=%s arquivos=%d pastas=%d bytes=%d "
            "inacessiveis=%d ignorados=%d tempo=%.1fs",
            status,
            total.files,
            total.folders,
            total.total_bytes,
            total.inaccessible,
            total.skipped,
            elapsed,
        )
        return ScanResult(
            analysis_id=self._store.analysis_id,
            status=status,
            files=total.files,
            folders=total.folders,
            total_bytes=total.total_bytes,
            inaccessible=total.inaccessible,
            skipped=total.skipped,
            elapsed_seconds=elapsed,
            has_warnings=has_warnings,
            error_message=error_message,
        )

    # Percurso ------------------------------------------------------------

    def _scan_location(self, location: ScanLocation, location_id: int) -> str:
        root_display = location.path
        root_fs = to_filesystem_path(root_display)
        self._set_current(root_display)

        try:
            root_stat = os.stat(root_fs, follow_symlinks=False)
        except OSError as exc:
            self._inaccessible(root_display, exc)
            return LOCATION_INACCESSIBLE

        link_kind = ISSUE_SYMLINK if stat.S_ISLNK(root_stat.st_mode) else None
        link_kind = link_kind or _reparse_link_kind(root_stat)
        if link_kind is not None and not is_volume_root(root_display):
            self._skip(link_kind, root_display)
            return LOCATION_COMPLETED
        if not stat.S_ISDIR(root_stat.st_mode):
            self._bump("inaccessible")
            self._store.add_issue(ISSUE_NOT_A_FOLDER, root_display, None)
            return LOCATION_INACCESSIBLE

        at_volume_root = is_volume_root(root_display)
        pending: list[tuple[str, str]] = [(root_fs, root_display)]
        is_root = True

        while pending:
            if self._cancel.is_set():
                return LOCATION_CANCELLED
            dir_fs, dir_display = pending.pop()
            self._set_current(dir_display)

            try:
                iterator = os.scandir(dir_fs)
            except OSError as exc:
                self._inaccessible(dir_display, exc)
                if is_root:
                    return LOCATION_INACCESSIBLE
                continue

            folder_id = self._store.add_folder(location_id, dir_display)
            skip_system = is_root and at_volume_root
            is_root = False
            subfolders: list[tuple[str, str]] = []

            with iterator:
                while not self._cancel.is_set():
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    except OSError as exc:
                        self._inaccessible(dir_display, exc)
                        break
                    self._process_entry(entry, dir_display, folder_id, skip_system, subfolders)
                    self._emit()

            # Ordem inversa: a pilha processa as subpastas em ordem alfabética do sistema.
            pending.extend(reversed(subfolders))
            self._store.maybe_flush()
            self._emit()

        return LOCATION_CANCELLED if self._cancel.is_set() else LOCATION_COMPLETED

    def _process_entry(
        self,
        entry: os.DirEntry,
        dir_display: str,
        folder_id: int,
        skip_system: bool,
        subfolders: list[tuple[str, str]],
    ) -> None:
        name = entry.name
        display = os.path.join(dir_display, name)
        try:
            link_kind = _entry_link_kind(entry)
            if link_kind is not None:
                self._skip(link_kind, display)
            elif entry.is_dir(follow_symlinks=False):
                if skip_system and name.casefold() in SYSTEM_ROOT_FOLDERS:
                    self._skip(ISSUE_SYSTEM_FOLDER, display)
                    return
                self._bump("folders")
                subfolders.append((entry.path, display))
            elif entry.is_file(follow_symlinks=False):
                st = entry.stat(follow_symlinks=False)
                size = int(st.st_size)
                self._store.add_file(
                    folder_id,
                    name,
                    os.path.splitext(name)[1][1:].lower(),
                    size,
                    _creation_time(st),
                    int(st.st_mtime),
                    int(st.st_atime),
                    getattr(st, "st_file_attributes", None),
                )
                self._bump("files")
                self._bump("total_bytes", size)
            else:
                self._skip(ISSUE_SPECIAL_ENTRY, display)
        except OSError as exc:
            self._inaccessible(display, exc)

    # Contadores e progresso ---------------------------------------------

    def _bump(self, field: str, amount: int = 1) -> None:
        setattr(self._total, field, getattr(self._total, field) + amount)
        setattr(self._loc, field, getattr(self._loc, field) + amount)

    def _inaccessible(self, path: str, exc: OSError) -> None:
        self._bump("inaccessible")
        self._store.add_issue(_issue_kind(exc), path, _issue_message(exc))

    def _skip(self, kind: str, path: str) -> None:
        self._bump("skipped")
        self._store.add_issue(kind, path, None)

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
        total = self._total
        self._on_progress(
            ScanProgress(
                files=total.files,
                folders=total.folders,
                total_bytes=total.total_bytes,
                inaccessible=total.inaccessible,
                skipped=total.skipped,
                current_path=self._current_path,
                elapsed_seconds=now - self._started,
            )
        )
