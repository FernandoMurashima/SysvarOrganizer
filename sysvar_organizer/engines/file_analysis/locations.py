"""Normalização dos locais selecionados e identificação de áreas protegidas."""

from __future__ import annotations

import os
import sys


def normalize_location(path: str) -> str:
    """Caminho absoluto e normalizado; raízes de unidade terminam em separador."""
    normalized = os.path.normpath(os.path.abspath(path))
    if sys.platform == "win32" and len(normalized) == 2 and normalized[1] == ":":
        normalized += "\\"
    return normalized


def _key(path: str) -> str:
    return os.path.normcase(normalize_location(path)).rstrip("\\/")


def is_same_or_inside(path: str, other: str) -> bool:
    """Indica se ``path`` é igual a ``other`` ou está dentro dele."""
    a, b = _key(path), _key(other)
    return a == b or a.startswith(b + os.sep)


def overlapping_location(path: str, selected: list[str]) -> str | None:
    """Retorna o local já selecionado que contém ``path`` ou está contido nele.

    Evita indexar os mesmos arquivos duas vezes na mesma análise.
    """
    for existing in selected:
        if is_same_or_inside(path, existing) or is_same_or_inside(existing, path):
            return existing
    return None


def is_volume_root(path: str) -> bool:
    normalized = normalize_location(path)
    return os.path.dirname(normalized) == normalized


def _windows_protected_dirs() -> list[str]:
    env = os.environ
    system_drive = env.get("SystemDrive", "C:") + "\\"
    candidates = [
        env.get("SystemRoot") or env.get("windir") or system_drive + "Windows",
        env.get("ProgramFiles"),
        env.get("ProgramFiles(x86)"),
        env.get("ProgramW6432"),
        env.get("ProgramData"),
        system_drive + "$Recycle.Bin",
        system_drive + "System Volume Information",
        system_drive + "Recovery",
    ]
    return [c for c in candidates if c]


def protected_location_warning(path: str) -> str | None:
    """Mensagem de aviso quando o local é ou contém pastas protegidas do Windows."""
    if sys.platform != "win32":
        return None
    for protected in _windows_protected_dirs():
        if is_same_or_inside(path, protected):
            return (
                f"O local selecionado fica dentro de uma pasta protegida do Windows "
                f"({protected}). Partes do conteúdo podem ter acesso negado; esses "
                f"itens serão registrados como inacessíveis e a análise continuará."
            )
        if is_same_or_inside(protected, path):
            return (
                "O local selecionado contém pastas protegidas do Windows (por exemplo, "
                f"{protected}). Itens com acesso negado serão registrados como "
                "inacessíveis e a análise continuará. A análise de uma unidade inteira "
                "pode levar bastante tempo."
            )
    return None
