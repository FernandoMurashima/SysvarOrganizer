"""Área de testes isolada — EXCLUSIVA dos testes automatizados.

Uma área de testes é uma pasta temporária criada pelo próprio conjunto de
testes (``IsolatedTestArea.create``), com um arquivo marcador contendo um
token aleatório. Este módulo NÃO é incluído no aplicativo distribuído, e o
executor recusa áreas de teste quando roda empacotado.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone

from sysvar_organizer.operations.safety import (
    ConfinedRoot,
    UnsafePath,
    check_plain_directory,
    path_key,
)

MARKER_NAME = ".sysvar-organizer-area-de-teste"


@dataclass(frozen=True)
class IsolatedTestArea(ConfinedRoot):
    root: str
    token: str
    reserved_names = (MARKER_NAME,)

    @classmethod
    def create(cls, parent: str | None = None) -> IsolatedTestArea:
        """Cria uma pasta temporária nova e vazia, marcada como área de testes."""
        _refuse_packaged()
        root = os.path.realpath(tempfile.mkdtemp(prefix="sysvar-organizer-teste-", dir=parent))
        token = secrets.token_hex(16)
        marker = os.path.join(root, MARKER_NAME)
        with open(marker, "x", encoding="utf-8") as handle:
            json.dump(
                {
                    "finalidade": "Área de testes isolada do Sysvar Organizer",
                    "token": token,
                    "criada_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
                handle,
            )
        return cls.open(root, token)

    @classmethod
    def open(cls, root: str, token: str) -> IsolatedTestArea:
        """Valida uma área existente: marcador presente e token correto."""
        _refuse_packaged()
        try:
            check_plain_directory(root)
        except UnsafePath as exc:
            code = {"area_link": "test_area_link", "area_missing": "test_area_missing",
                    "area_protected": "test_area_protected"}.get(exc.code, "test_area_invalid")
            raise UnsafePath(code, f"Área de testes recusada: {exc.message}") from exc
        normalized = os.path.normpath(root)
        if path_key(normalized) == path_key(os.path.expanduser("~")):
            raise UnsafePath("test_area_invalid", "A pasta do usuário não pode ser área de testes.")
        marker = os.path.join(normalized, MARKER_NAME)
        try:
            if not os.path.isfile(marker) or os.path.islink(marker):
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

    def destroy(self) -> None:
        """Remove a área de testes (somente com marcador e token válidos)."""
        IsolatedTestArea.open(self.root, self.token)
        shutil.rmtree(self.root)


def _refuse_packaged() -> None:
    if getattr(sys, "frozen", False):
        raise UnsafePath("packaged_build", "Áreas de teste não existem no aplicativo distribuído.")
