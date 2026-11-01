"""Verificação rápida de inicialização (``--self-check``).

Confirma importação dos módulos, abertura e redimensionamento da janela,
navegação entre páginas e acesso ao banco SQLite. Não lê arquivos do usuário
e não grava registros de teste no banco.
"""

from __future__ import annotations

import importlib

from PySide6.QtWidgets import QApplication

from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.persistence.database import LATEST_SCHEMA_VERSION
from sysvar_organizer.ui.main_window import PAGES, MainWindow

MODULES = (
    "sysvar_organizer",
    "sysvar_organizer.app",
    "sysvar_organizer.context",
    "sysvar_organizer.config.paths",
    "sysvar_organizer.config.settings",
    "sysvar_organizer.core.errors",
    "sysvar_organizer.core.logging_setup",
    "sysvar_organizer.persistence.database",
    "sysvar_organizer.persistence.repositories",
    "sysvar_organizer.engines.file_analysis",
    "sysvar_organizer.engines.file_analysis.locations",
    "sysvar_organizer.engines.file_analysis.models",
    "sysvar_organizer.engines.file_analysis.scanner",
    "sysvar_organizer.persistence.scan_store",
    "sysvar_organizer.engines.comparison",
    "sysvar_organizer.engines.visual_analysis",
    "sysvar_organizer.operations",
    "sysvar_organizer.ui.theme",
    "sysvar_organizer.ui.widgets",
    "sysvar_organizer.ui.main_window",
    "sysvar_organizer.ui.pages.dashboard",
    "sysvar_organizer.ui.pages.unavailable",
    "sysvar_organizer.ui.pages.storage",
    "sysvar_organizer.ui.formatting",
    "sysvar_organizer.ui.drive_dialog",
    "sysvar_organizer.ui.scan_worker",
)


def run_self_check(app: QApplication, window: MainWindow, context: AppContext) -> int:
    window.persist_geometry = False
    results: list[tuple[str, bool, str]] = []

    failed = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name} ({exc.__class__.__name__})")
    results.append(
        ("Importação dos módulos", not failed, ", ".join(failed) or f"{len(MODULES)} módulos")
    )

    window.showNormal()
    app.processEvents()
    results.append(("Abertura da janela", window.isVisible(), window.windowTitle()))

    resize_ok = True
    details = []
    for width, height in ((1000, 640), (1200, 720)):
        window.resize(width, height)
        app.processEvents()
        ok = window.width() == width and window.height() == height
        resize_ok = resize_ok and ok
        details.append(f"{width}x{height}->{window.width()}x{window.height()}")
    window.resize(400, 300)
    app.processEvents()
    min_ok = window.width() >= 960 and window.height() >= 600
    resize_ok = resize_ok and min_ok
    details.append(f"mínimo {window.width()}x{window.height()}")
    results.append(("Redimensionamento", resize_ok, "; ".join(details)))

    nav_ok = True
    for index, spec in enumerate(PAGES):
        window._nav.setCurrentRow(index)  # noqa: SLF001 - verificação interna
        app.processEvents()
        nav_ok = nav_ok and window.current_page_key() == spec.key
        nav_ok = nav_ok and window._stack.currentIndex() == index  # noqa: SLF001
    window._nav.setCurrentRow(0)  # noqa: SLF001
    results.append(("Navegação do menu lateral", nav_ok, f"{len(PAGES)} páginas"))

    if context.database is None or context.history is None:
        results.append(("Banco SQLite", False, context.database_error or "indisponível"))
    else:
        try:
            version = context.database.schema_version()
            total = context.history.count()
        except DatabaseError as exc:
            results.append(("Banco SQLite", False, str(exc)))
        else:
            results.append(
                (
                    "Banco SQLite",
                    version == LATEST_SCHEMA_VERSION,
                    f"{context.database_path} (esquema v{version}, {total} análises)",
                )
            )

    window.close()
    app.processEvents()

    for name, ok, detail in results:
        print(f"[{'OK' if ok else 'FALHA'}] {name}: {detail}")
    all_ok = all(ok for _, ok, _ in results)
    print("Resultado:", "aprovado" if all_ok else "com falhas")
    return 0 if all_ok else 1
