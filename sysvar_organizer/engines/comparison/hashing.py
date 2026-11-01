"""Revalidação de arquivos indexados e cálculo de SHA-256, com reaproveitamento.

Componente compartilhado pela busca de duplicados (Bloco 03) e pela
comparação de pastas (Bloco 04). Regras:

* Antes de qualquer leitura, o arquivo é revalidado: existe, é arquivo comum,
  não é link/junção/ponto de montagem, tem o mesmo tamanho e data de
  modificação da indexação e não está disponível somente na nuvem.
* Um SHA-256 anterior só é reaproveitado se tamanho, data de modificação em
  nanossegundos, volume e identificador do arquivo atuais coincidirem.
* O conteúdo é lido em blocos de 1 MiB, somente leitura. Alterações durante a
  leitura invalidam o resultado.
"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Callable
from typing import Protocol

from sysvar_organizer.engines.file_analysis.scanner import (
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_SYMLINK,
    to_filesystem_path,
)

CHUNK_SIZE = 1024 * 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# Atributos do Windows de arquivos que estão apenas na nuvem (OneDrive etc.).
# Ler o conteúdo provocaria o download, por isso esses arquivos são ignorados.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY_ATTRIBUTES = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

ISSUE_MISSING = "missing"
ISSUE_MODIFIED = "modified_since_scan"
ISSUE_CHANGED_DURING_READ = "changed_during_read"
ISSUE_ACCESS_DENIED = "access_denied"
ISSUE_READ_ERROR = "read_error"
ISSUE_LINK = "link_not_followed"
ISSUE_NOT_REGULAR = "not_regular_file"
ISSUE_CLOUD_ONLY = "cloud_only"


class Unavailable(Exception):
    """O arquivo não pode ser comparado com segurança."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class Cancelled(Exception):
    """Cancelamento solicitado durante a leitura."""


class HashCache(Protocol):
    def cached_hash(
        self, path_key: str, size: int, modified_ns: int, volume_id: str, file_index: str
    ) -> str | None: ...

    def save_hash(
        self,
        path_key: str,
        size: int,
        modified_ns: int,
        volume_id: str,
        file_index: str,
        sha256: str,
    ) -> None: ...


def path_key(path: str) -> str:
    return os.path.normcase(path)


def physical_key(st: os.stat_result, row_id: int) -> str:
    """Identifica o arquivo físico. Sem identificador do sistema, cada caminho é único."""
    if st.st_ino:
        return f"{st.st_dev}:{st.st_ino}"
    return f"row:{row_id}"


def os_issue(exc: OSError) -> Unavailable:
    if isinstance(exc, FileNotFoundError):
        return Unavailable(ISSUE_MISSING, "Arquivo não encontrado (removido ou renomeado).")
    if isinstance(exc, PermissionError):
        return Unavailable(ISSUE_ACCESS_DENIED, "Acesso negado.")
    detail = exc.strerror or exc.__class__.__name__
    return Unavailable(ISSUE_READ_ERROR, f"Falha de leitura: {detail}")


class FileVerifier:
    def __init__(
        self,
        cache: HashCache,
        cancel_event: threading.Event,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        self._cache = cache
        self._cancel = cancel_event
        self._on_read = on_read
        self.read_count = 0  # arquivos com SHA-256 calculado agora
        self.reused = 0  # arquivos com SHA-256 reaproveitado
        self.bytes_read = 0

    def validate(self, full_path: str, size: int, modified_at: int | None) -> os.stat_result:
        """Confere o arquivo atual contra os dados da indexação."""
        try:
            st = os.stat(to_filesystem_path(full_path), follow_symlinks=False)
        except OSError as exc:
            raise os_issue(exc) from exc

        tag = getattr(st, "st_reparse_tag", 0)
        if stat.S_ISLNK(st.st_mode) or tag in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT):
            raise Unavailable(ISSUE_LINK, "Link simbólico ou junção: não seguido.")
        if not stat.S_ISREG(st.st_mode):
            raise Unavailable(ISSUE_NOT_REGULAR, "Não é um arquivo comum.")
        if modified_at is None or st.st_size != size or int(st.st_mtime) != modified_at:
            raise Unavailable(
                ISSUE_MODIFIED, "Tamanho ou data de modificação diferentes dos indexados."
            )
        if getattr(st, "st_file_attributes", 0) & CLOUD_ONLY_ATTRIBUTES:
            raise Unavailable(ISSUE_CLOUD_ONLY, "Disponível apenas na nuvem; não foi baixado.")
        return st

    def sha256(self, full_path: str, st: os.stat_result, size: int) -> tuple[str, bool]:
        """SHA-256 do arquivo validado. Retorna (hash, reaproveitado)."""
        key = path_key(full_path)
        volume_id, file_index = str(st.st_dev), str(st.st_ino)
        if st.st_ino:
            cached = self._cache.cached_hash(key, size, st.st_mtime_ns, volume_id, file_index)
            if cached is not None:
                self.reused += 1
                return cached, True

        if size == 0:
            digest = EMPTY_SHA256
        else:
            digest = self._read_sha256(full_path, st, size)
        self.read_count += 1
        if st.st_ino:
            self._cache.save_hash(key, size, st.st_mtime_ns, volume_id, file_index, digest)
        return digest, False

    def _read_sha256(self, full_path: str, st: os.stat_result, size: int) -> str:
        digest = hashlib.sha256()
        buffer = bytearray(CHUNK_SIZE)
        view = memoryview(buffer)
        total = 0
        try:
            with open(to_filesystem_path(full_path), "rb", buffering=0) as handle:
                before = os.fstat(handle.fileno())
                if before.st_size != st.st_size or before.st_mtime_ns != st.st_mtime_ns:
                    raise Unavailable(ISSUE_CHANGED_DURING_READ, "Arquivo alterado antes da leitura.")
                while True:
                    if self._cancel.is_set():
                        raise Cancelled
                    count = handle.readinto(buffer)
                    if not count:
                        break
                    digest.update(view[:count])
                    total += count
                    self.bytes_read += count
                    if self._on_read is not None:
                        self._on_read()
                after = os.fstat(handle.fileno())
        except OSError as exc:
            raise os_issue(exc) from exc

        if total != size or after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
            raise Unavailable(ISSUE_CHANGED_DURING_READ, "Arquivo alterado durante a leitura.")
        return digest.hexdigest()
