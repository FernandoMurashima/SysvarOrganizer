# -*- mode: python ; coding: utf-8 -*-
# Especificação do PyInstaller para o Sysvar Organizer (Windows 11, 64 bits).
#
# Modo "pasta única" (onedir): o executável fica ao lado das bibliotecas Qt,
# que permanecem como DLLs separadas e substituíveis (relevante para a LGPL).
# Use packaging\build_windows.cmd, que gera o arquivo de versão e valida o
# resultado; esta especificação não deve ser executada diretamente.
#
# Segurança (Bloco 10): o pacote inclui o executor físico e a política de
# autorização. Copiar, mover e reverter só acontecem dentro de áreas de
# homologação autorizadas pelo usuário; o executor confere a autorização por
# conta própria. A área de testes isolada (exclusiva dos testes automatizados)
# é EXCLUÍDA do pacote. Não há exclusão de arquivos; Lixeira desabilitada.

import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 - definido pelo PyInstaller
PACKAGE = ROOT / "sysvar_organizer"
APP_EXE_NAME = "SysvarOrganizer"

VERSION_FILE = os.environ.get("SYSVAR_ORGANIZER_VERSION_FILE", "")
if not VERSION_FILE or not Path(VERSION_FILE).is_file():
    raise SystemExit(
        "Arquivo de versão do executável ausente. Gere o pacote com packaging\\build_windows.cmd."
    )

# Nunca distribuídos: área de testes isolada dos testes automatizados.
TEST_ONLY_MODULES = [
    "sysvar_organizer.operations.test_area",
]
# Módulos Qt não utilizados (o aplicativo usa apenas QtCore, QtGui e QtWidgets).
QT_EXCLUDES = [
    "PySide6.Qt3DCore",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtNetworkAuth",
    "PySide6.QtPdf",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtSql",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]
OTHER_EXCLUDES = ["tkinter", "_tkinter", "pytest", "IPython"]


def package_modules():
    """Todos os módulos do aplicativo, exceto os exclusivos de teste.

    Lidos da árvore de código (sem importar nada), pois o diagnóstico importa
    módulos por nome e o PyInstaller não os detectaria sozinho.
    """
    names = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(parts)
        if name in TEST_ONLY_MODULES or name.endswith(".__main__"):
            continue
        names.append(name)
    return names


a = Analysis(  # noqa: F821
    [str(Path(SPECPATH) / "entry.py")],  # noqa: F821
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=package_modules(),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=TEST_ONLY_MODULES + QT_EXCLUDES + OTHER_EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # sem janela de terminal ao abrir
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="NONE",  # o repositório não tem ícone próprio
    version=VERSION_FILE,
    uac_admin=False,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_EXE_NAME,
)
