"""Política de autorização das operações reais (primeira fase: homologação).

Operações físicas reais só acontecem dentro de ÁREAS AUTORIZADAS: pastas que
o próprio usuário escolheu e autorizou explicitamente como área de
homologação. Nenhum disco é liberado automaticamente.

Regras para autorizar uma pasta:

* pasta comum, existente, com caminho completo, sem links/junções no caminho
  e fora de locais protegidos do sistema (regras gerais de ``safety``);
* não pode ser a raiz de uma unidade, a pasta do usuário nem uma pasta que
  CONTENHA a pasta do usuário; não pode ser compartilhamento de rede;
* não pode conter nem ficar dentro da pasta de dados do Organizer nem da
  pasta do programa;
* não pode se sobrepor a outra área ativa.

A identidade física da pasta (volume + índice) é gravada na autorização e
reconferida a cada uso: se a pasta for trocada, renomeada para outro lugar,
substituída por link ou a unidade mudar de letra, a área deixa de valer.
Uma área revogada nunca volta a valer (é preciso autorizar de novo).

Planos só podem ser executados quando foram CRIADOS vinculados a áreas
ativas; planos antigos (sem área) permanecem bloqueados.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sysvar_organizer.operations.safety import (
    ConfinedRoot,
    UnsafePath,
    check_plain_directory,
    is_link_like,
    is_within,
    path_key,
)

AREA_ACTIVE = "active"
AREA_REVOKED = "revoked"


@dataclass(frozen=True)
class AuthorizedArea(ConfinedRoot):
    id: int
    root: str
    volume_id: str
    dir_index: str
    status: str = AREA_ACTIVE

    def verify(self) -> AuthorizedArea:
        """Confere que a área continua ativa e é a MESMA pasta autorizada."""
        if self.status != AREA_ACTIVE:
            raise UnsafePath("area_revoked", "A autorização desta área foi revogada.")
        st = check_plain_directory(self.root)
        if is_link_like(st) or (str(st.st_dev), str(st.st_ino)) != (self.volume_id, self.dir_index):
            raise UnsafePath(
                "area_changed",
                "A pasta autorizada foi trocada, movida ou não é mais a mesma; autorize-a novamente.",
            )
        return self


def application_folders() -> list[str]:
    """Pastas do próprio Organizer que nunca podem ser áreas de operação."""
    from sysvar_organizer.config import paths

    folders = [str(paths.user_data_dir())]
    if getattr(sys, "frozen", False):
        folders.append(str(Path(sys.executable).resolve().parent))
    folders.append(str(Path(__file__).resolve().parents[2]))  # código do aplicativo
    return folders


def check_area_root(
    root: str, active_roots: Iterable[str] = (), protected: Iterable[str] | None = None
) -> tuple[str, str, str]:
    """Valida uma pasta candidata. Retorna ``(raiz, volume, índice)``."""
    normalized = os.path.normpath(root) if root else root
    if normalized and (normalized.startswith("\\\\") or normalized.startswith("//")):
        raise UnsafePath("area_network", "Pastas de rede não são aceitas nesta fase de homologação.")
    st = check_plain_directory(normalized)
    home = os.path.expanduser("~")
    if path_key(normalized) == path_key(home) or is_within(normalized, home):
        raise UnsafePath(
            "area_too_broad",
            "A pasta do usuário (ou uma pasta que a contém) não pode ser autorizada inteira; "
            "escolha uma subpasta específica para homologação.",
        )
    for folder in application_folders() if protected is None else protected:
        if is_within(normalized, folder) or is_within(folder, normalized):
            raise UnsafePath(
                "area_application", "A pasta de dados ou do programa do Organizer não pode ser usada."
            )
    for other in active_roots:
        if is_within(other, normalized) or is_within(normalized, other):
            raise UnsafePath("area_overlap", "A pasta se sobrepõe a outra área já autorizada.")
    return normalized, str(st.st_dev), str(st.st_ino)


def area_for(path: str, areas: Iterable[AuthorizedArea]) -> AuthorizedArea | None:
    """Área ativa que contém ``path`` (ou ``None``)."""
    for area in areas:
        if area.status == AREA_ACTIVE and area.contains(path):
            return area
    return None
