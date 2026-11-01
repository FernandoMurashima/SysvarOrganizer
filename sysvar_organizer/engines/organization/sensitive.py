"""Detecção de estruturas sensíveis, somente pelos nomes já indexados.

Estruturas cujo funcionamento depende de caminhos relativos não devem ser
desmontadas arquivo a arquivo: projetos de software, ambientes virtuais,
controle de versão, aplicativos instalados, bancos de dados e pastas de
produtos Sysvar. Quando há evidência (um marcador conhecido), a pasta inteira
é sinalizada: seus arquivos nunca recebem destino "válido" e podem ser
excluídos da proposta.

A detecção usa apenas nomes de pastas e arquivos do conjunto selecionado;
nenhum arquivo é aberto e nada fora do conjunto é consultado.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

KIND_PROJECT = "project"
KIND_ENVIRONMENT = "environment"
KIND_VCS = "version_control"
KIND_APPLICATION = "application"
KIND_DATABASE = "database"
KIND_SYSVAR = "sysvar_product"

KIND_LABELS = {
    KIND_PROJECT: "Projeto de software",
    KIND_ENVIRONMENT: "Ambiente de execução / dependências",
    KIND_VCS: "Controle de versão",
    KIND_APPLICATION: "Aplicativo instalado",
    KIND_DATABASE: "Banco de dados",
    KIND_SYSVAR: "Produto Sysvar",
}

# Pastas cujo próprio nome é marcador; a estrutura protegida é a pasta-mãe
# (para controle de versão) ou a própria pasta (para ambientes).
VCS_FOLDERS = {".git": KIND_VCS, ".svn": KIND_VCS, ".hg": KIND_VCS}
ENVIRONMENT_FOLDERS = {
    "node_modules", ".venv", "venv", "site-packages", "__pycache__", ".tox",
    "vendor", "bower_components", ".gradle", ".idea", ".vscode",
}

# Arquivos marcadores: a pasta que os contém é a estrutura protegida.
PROJECT_FILES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "pipfile", "cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "cmakelists.txt", "makefile", "composer.json", "gemfile",
    "angular.json", "manage.py", "pubspec.yaml", "tsconfig.json", "claude.md",
}
PROJECT_EXTENSIONS = {"sln", "csproj", "vbproj", "vcxproj", "xcodeproj", "uproject"}
ENVIRONMENT_FILES = {"pyvenv.cfg"}
DATABASE_FILES = {"pg_version", "ibdata1", "mysql.ibd"}
DATABASE_EXTENSIONS = {"mdf", "ldf", "ndf", "ibd", "frm", "myd", "myi"}
SYSVAR_PREFIX = "sysvar"

# Aplicativo: pasta com ao menos um .exe e várias bibliotecas .dll.
APPLICATION_MIN_DLLS = 3


@dataclass(frozen=True)
class Structure:
    path: str  # caminho completo da pasta protegida
    kind: str
    reason: str


def detect(
    folders: Iterable[str],
    files: Iterable[tuple[str, str]],
    root: str,
) -> dict[str, Structure]:
    """Estruturas sensíveis no conjunto.

    ``folders``: caminhos completos das pastas indexadas do conjunto.
    ``files``: (caminho da pasta, nome do arquivo) dos arquivos do conjunto.
    Retorna {chave normalizada da pasta: Structure}.
    """
    found: dict[str, Structure] = {}
    root_key = _key(root)

    def add(path: str, kind: str, reason: str) -> None:
        key = _key(path)
        if key not in found:
            found[key] = Structure(path, kind, reason)

    for path in folders:
        name = os.path.basename(path.rstrip("\\/"))
        lower = name.lower()
        if lower in VCS_FOLDERS:
            parent = os.path.dirname(path.rstrip("\\/"))
            add(parent, KIND_VCS, f"Contém a pasta de controle de versão '{name}'.")
        elif lower in ENVIRONMENT_FOLDERS:
            add(path, KIND_ENVIRONMENT, f"Pasta de ambiente/dependências '{name}'.")
        if lower.startswith(SYSVAR_PREFIX) and _key(path) != root_key:
            add(path, KIND_SYSVAR, f"Pasta de produto Sysvar ('{name}').")

    exe_count: dict[str, int] = {}
    dll_count: dict[str, int] = {}
    folder_names: dict[str, str] = {}
    for folder, name in files:
        lower = name.lower()
        extension = os.path.splitext(lower)[1][1:]
        key = _key(folder)
        folder_names[key] = folder
        if lower in PROJECT_FILES or extension in PROJECT_EXTENSIONS:
            add(folder, KIND_PROJECT, f"Contém o arquivo de projeto '{name}'.")
        elif lower in ENVIRONMENT_FILES:
            add(folder, KIND_ENVIRONMENT, f"Contém '{name}' (ambiente virtual).")
        elif lower in DATABASE_FILES or extension in DATABASE_EXTENSIONS:
            add(folder, KIND_DATABASE, f"Contém arquivos de banco de dados ('{name}').")
        if extension == "exe":
            exe_count[key] = exe_count.get(key, 0) + 1
        elif extension == "dll":
            dll_count[key] = dll_count.get(key, 0) + 1

    for key, exes in exe_count.items():
        if dll_count.get(key, 0) >= APPLICATION_MIN_DLLS:
            add(
                folder_names[key],
                KIND_APPLICATION,
                f"Contém {exes} executável(is) e {dll_count[key]} bibliotecas (.dll).",
            )
    return found


def find_protecting(folder: str, structures: dict[str, Structure]) -> Structure | None:
    """A estrutura sensível mais próxima que contém ``folder`` (ou None)."""
    if not structures:
        return None
    current = folder.rstrip("\\/") or folder
    while True:
        structure = structures.get(_key(current))
        if structure is not None:
            return structure
        parent = os.path.dirname(current)
        if not parent or parent == current:
            return None
        current = parent


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))
