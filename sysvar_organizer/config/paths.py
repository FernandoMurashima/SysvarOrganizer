"""Locais de dados do usuário.

O banco de dados e os logs ficam no diretório local de dados do usuário,
fora do repositório:

* Windows: ``%LOCALAPPDATA%\\Sysvar\\Organizer``
* Outros sistemas: ``$XDG_DATA_HOME/sysvar-organizer`` (padrão ``~/.local/share``)

A variável de ambiente ``SYSVAR_ORGANIZER_DATA_DIR`` substitui o local padrão,
permitindo executar verificações com um diretório isolado.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DATA_DIR_ENV = "SYSVAR_ORGANIZER_DATA_DIR"
DATABASE_FILENAME = "organizer.db"
LOG_FILENAME = "organizer.log"


def user_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Sysvar" / "Organizer"

    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / "sysvar-organizer"


def database_path() -> Path:
    return user_data_dir() / DATABASE_FILENAME


def log_dir() -> Path:
    return user_data_dir() / "logs"


def log_path() -> Path:
    return log_dir() / LOG_FILENAME


def ensure_data_dirs() -> Path:
    """Cria o diretório de dados e o de logs, se ainda não existirem."""
    data_dir = user_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
    return data_dir
