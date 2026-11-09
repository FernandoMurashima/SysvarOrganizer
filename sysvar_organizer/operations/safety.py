"""Regras GERAIS de segurança de caminhos para operações físicas.

Valem para qualquer raiz confinada — área de homologação autorizada pelo
usuário (``authorization``) ou área de testes isolada (``test_area``, usada
apenas pelos testes automatizados e excluída do aplicativo distribuído).

Um caminho só é aceito se:

* é absoluto e, normalizado, fica dentro da raiz (inclusive contra '..');
* nenhum componente existente entre a raiz e o arquivo é link simbólico,
  junção ou ponto de montagem;
* o diretório real (resolvido) continua dentro da raiz;
* não é um local protegido do sistema nem um nome reservado da raiz.

A validação é refeita imediatamente antes de cada abertura ou alteração.
"""

from __future__ import annotations

import os
import stat

from sysvar_organizer.engines.file_analysis.locations import protected_location_warning
from sysvar_organizer.engines.file_analysis.scanner import (
    IO_REPARSE_TAG_MOUNT_POINT,
    IO_REPARSE_TAG_SYMLINK,
)

_LINK_TAGS = (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)


class UnsafePath(Exception):
    """Caminho ou área recusados por segurança."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_link_like(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def is_within(root: str, path: str) -> bool:
    """``path`` fica dentro de ``root`` (ou é a própria raiz)?"""
    try:
        common = os.path.commonpath([path_key(root), path_key(path)])
    except ValueError:  # unidades diferentes
        return False
    return common == path_key(root)


class ConfinedRoot:
    """Raiz confinada: todo caminho operado precisa estar dentro dela."""

    root: str
    reserved_names: tuple[str, ...] = ()

    def contains(self, path: str) -> bool:
        return is_within(self.root, path)

    def resolve(self, path: str) -> str:
        """Caminho normalizado e seguro dentro da raiz (o alvo pode não existir).

        Recusa caminhos relativos, fora da raiz (inclusive por '..') e qualquer
        componente existente que seja link simbólico, junção ou ponto de montagem.
        """
        if not path or not os.path.isabs(path):
            raise UnsafePath("relative_path", "Caminho relativo não é permitido.")
        normalized = os.path.normpath(path)
        if not self.contains(normalized) or path_key(normalized) == path_key(self.root):
            raise UnsafePath("outside_area", "Caminho fora da área autorizada.")
        if os.path.basename(normalized) in self.reserved_names:
            raise UnsafePath("outside_area", "Arquivo reservado da área não pode ser alterado.")
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
        if not self.contains(real_parent):
            raise UnsafePath("outside_area", "O caminho real fica fora da área autorizada.")
        if protected_location_warning(normalized):
            raise UnsafePath("protected_location", "Caminho em local protegido do sistema.")
        return normalized


def check_plain_directory(root: str) -> os.stat_result:
    """Pasta comum, absoluta, sem links no caminho, fora de locais protegidos.

    Regras comuns a qualquer raiz confinada. Retorna o ``lstat`` da pasta.
    """
    if not root or not os.path.isabs(root):
        raise UnsafePath("area_invalid", "A pasta deve ser informada com caminho completo.")
    normalized = os.path.normpath(root)
    if path_key(os.path.realpath(normalized)) != path_key(normalized):
        raise UnsafePath("area_link", "A pasta não pode passar por links, junções ou atalhos.")
    try:
        st = os.lstat(normalized)
    except OSError as exc:
        raise UnsafePath("area_missing", "A pasta não existe ou não está acessível.") from exc
    if not stat.S_ISDIR(st.st_mode) or is_link_like(st):
        raise UnsafePath("area_invalid", "O caminho não é uma pasta comum.")
    if os.path.dirname(normalized) == normalized:
        raise UnsafePath("area_invalid", "A raiz de uma unidade não pode ser usada.")
    if protected_location_warning(normalized):
        raise UnsafePath("area_protected", "A pasta está em um local protegido do sistema.")
    return st
