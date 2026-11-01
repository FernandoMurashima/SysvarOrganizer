"""Área de testes isolada e validação de caminhos para operações físicas.

Uma área de testes é uma pasta temporária criada pelo próprio conjunto de
testes (``IsolatedTestArea.create``), com um arquivo marcador contendo um
token aleatório. O executor só aceita uma área aberta com o token correto e
só opera em caminhos que:

* são absolutos e, normalizados, ficam dentro da raiz da área;
* não atravessam links simbólicos, junções nem pontos de montagem em nenhum
  componente entre a raiz e o arquivo;
* não ficam em pastas protegidas do sistema.

A validação é refeita imediatamente antes de cada abertura ou alteração.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from sysvar_organizer.engines.file_analysis.locations import protected_location_warning
from sysvar_organizer.engines.file_analysis.scanner import (
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_SYMLINK,
)

MARKER_NAME = ".sysvar-organizer-area-de-teste"
_LINK_TAGS = (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)


class UnsafePath(Exception):
    """Caminho ou área recusados por segurança."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_link_like(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


@dataclass(frozen=True)
class IsolatedTestArea:
    root: str
    token: str

    # Criação e abertura --------------------------------------------------

    @classmethod
    def create(cls, parent: str | None = None) -> IsolatedTestArea:
        """Cria uma pasta temporária nova e vazia, marcada como área de testes."""
        root = os.path.realpath(tempfile.mkdtemp(prefix="sysvar-organizer-teste-", dir=parent))
        token = secrets.token_hex(16)
        marker = os.path.join(root, MARKER_NAME)
        with open(marker, "x", encoding="utf-8") as handle:
            json.dump(
                {
                    "finalidade": "Área de testes isolada do Sysvar Organizer (Bloco 07)",
                    "token": token,
                    "criada_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                handle,
            )
        return cls.open(root, token)

    @classmethod
    def open(cls, root: str, token: str) -> IsolatedTestArea:
        """Valida uma área existente: marcador presente e token correto."""
        if not root or not os.path.isabs(root):
            raise UnsafePath("test_area_invalid", "A raiz da área de testes deve ser absoluta.")
        normalized = os.path.normpath(root)
        if _key(os.path.realpath(normalized)) != _key(normalized):
            raise UnsafePath(
                "test_area_link", "A raiz da área de testes não pode passar por links."
            )
        try:
            st = os.lstat(normalized)
        except OSError as exc:
            raise UnsafePath("test_area_missing", "A área de testes não existe.") from exc
        if not stat.S_ISDIR(st.st_mode) or is_link_like(st):
            raise UnsafePath("test_area_invalid", "A área de testes não é uma pasta comum.")
        if os.path.dirname(normalized) == normalized:
            raise UnsafePath("test_area_invalid", "A raiz de uma unidade não pode ser área de testes.")
        if _key(normalized) == _key(os.path.expanduser("~")):
            raise UnsafePath("test_area_invalid", "A pasta do usuário não pode ser área de testes.")
        if protected_location_warning(normalized):
            raise UnsafePath("test_area_protected", "A área de testes está em local protegido.")
        marker = os.path.join(normalized, MARKER_NAME)
        try:
            marker_stat = os.lstat(marker)
            if not stat.S_ISREG(marker_stat.st_mode):
                raise OSError("marcador inválido")
            with open(marker, encoding="utf-8") as handle:
                stored = json.load(handle).get("token")
        except (OSError, ValueError) as exc:
            raise UnsafePath(
                "test_area_unmarked", "A pasta não é uma área de testes do Organizer."
            ) from exc
        if not stored or not secrets.compare_digest(str(stored), token):
            raise UnsafePath("test_area_token", "Token da área de testes não confere.")
        return cls(normalized, token)

    # Caminhos ------------------------------------------------------------

    def contains(self, path: str) -> bool:
        try:
            common = os.path.commonpath([_key(self.root), _key(path)])
        except ValueError:  # unidades diferentes
            return False
        return common == _key(self.root)

    def resolve(self, path: str) -> str:
        """Caminho normalizado e seguro dentro da área (o alvo pode não existir).

        Recusa caminhos relativos, fora da raiz (inclusive por '..') e qualquer
        componente existente que seja link simbólico, junção ou ponto de montagem.
        """
        if not path or not os.path.isabs(path):
            raise UnsafePath("relative_path", "Caminho relativo não é permitido.")
        normalized = os.path.normpath(path)
        if not self.contains(normalized) or _key(normalized) == _key(self.root):
            raise UnsafePath("outside_area", "Caminho fora da área autorizada.")
        if os.path.basename(normalized) == MARKER_NAME:
            raise UnsafePath("outside_area", "O marcador da área não pode ser alterado.")
        relative = os.path.relpath(normalized, self.root)
        current = self.root
        for part in relative.split(os.sep):
            current = os.path.join(current, part)
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                break  # o restante ainda não existe
            if is_link_like(st):
                raise UnsafePath(
                    "link_in_path", "Links simbólicos, junções e pontos de montagem não são seguidos."
                )
        real_parent = os.path.realpath(os.path.dirname(normalized))
        if not self.contains(real_parent) and _key(real_parent) != _key(self.root):
            raise UnsafePath("outside_area", "O caminho real fica fora da área autorizada.")
        if protected_location_warning(normalized):
            raise UnsafePath("protected_location", "Destino em local protegido do sistema.")
        return normalized

    def destroy(self) -> None:
        """Remove a área de testes (somente com marcador e token válidos)."""
        IsolatedTestArea.open(self.root, self.token)
        shutil.rmtree(self.root)
