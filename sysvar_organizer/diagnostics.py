"""Verificação rápida de inicialização (``--self-check``).

Confirma importação dos módulos, abertura e redimensionamento da janela,
navegação entre páginas, acesso ao banco SQLite, integridade rápida e o
bloqueio das operações reais. É somente leitura: não lê arquivos do usuário,
não cria, migra nem altera o banco e não grava a geometria da janela.

O resultado é impresso no console (quando houver) e gravado em
``logs/self-check.txt``; no aplicativo instalado, sem console, também é
apresentado numa janela de aviso.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from sysvar_organizer import APP_NAME, __version__
from sysvar_organizer.config import paths
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.persistence.database import LATEST_SCHEMA_VERSION, Database
from sysvar_organizer.ui.main_window import PAGES, MainWindow

REPORT_FILE_NAME = "self-check.txt"
# Só existe no ambiente de desenvolvimento/testes: a área de testes isolada
# NÃO é incluída no aplicativo distribuído.
TEST_ONLY_MODULES = ("sysvar_organizer.operations.test_area",)

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
    "sysvar_organizer.engines.comparison.models",
    "sysvar_organizer.engines.comparison.duplicates",
    "sysvar_organizer.engines.comparison.hashing",
    "sysvar_organizer.engines.comparison.folders",
    "sysvar_organizer.engines.visual_analysis",
    "sysvar_organizer.engines.visual_analysis.models",
    "sysvar_organizer.engines.visual_analysis.signatures",
    "sysvar_organizer.engines.visual_analysis.grouping",
    "sysvar_organizer.engines.visual_analysis.similar",
    "sysvar_organizer.engines.organization",
    "sysvar_organizer.engines.organization.categories",
    "sysvar_organizer.engines.organization.rules",
    "sysvar_organizer.engines.organization.sensitive",
    "sysvar_organizer.engines.organization.models",
    "sysvar_organizer.engines.organization.planner",
    "sysvar_organizer.persistence.organization_store",
    "sysvar_organizer.persistence.organization_repository",
    "sysvar_organizer.persistence.visual_store",
    "sysvar_organizer.persistence.visual_repository",
    "sysvar_organizer.persistence.hash_cache",
    "sysvar_organizer.persistence.comparison_store",
    "sysvar_organizer.persistence.comparison_repository",
    "sysvar_organizer.persistence.duplicate_store",
    "sysvar_organizer.persistence.duplicate_repository",
    "sysvar_organizer.engines.visual_analysis",
    "sysvar_organizer.operations",
    "sysvar_organizer.operations.models",
    "sysvar_organizer.operations.safety",
    "sysvar_organizer.operations.authorization",
    "sysvar_organizer.operations.associations",
    "sysvar_organizer.operations.planning",
    "sysvar_organizer.operations.executor",
    "sysvar_organizer.operations.runner",
    "sysvar_organizer.operations.recycle",
    "sysvar_organizer.operations.consolidation",
    "sysvar_organizer.persistence.consolidation_store",
    "sysvar_organizer.persistence.operations_store",
    "sysvar_organizer.persistence.history_repository",
    "sysvar_organizer.reports",
    "sysvar_organizer.reports.values",
    "sysvar_organizer.reports.model",
    "sysvar_organizer.reports.catalog",
    "sysvar_organizer.reports.export",
    "sysvar_organizer.ui.theme",
    "sysvar_organizer.ui.widgets",
    "sysvar_organizer.ui.main_window",
    "sysvar_organizer.ui.pages.dashboard",
    "sysvar_organizer.ui.pages.unavailable",
    "sysvar_organizer.ui.pages.storage",
    "sysvar_organizer.ui.formatting",
    "sysvar_organizer.ui.drive_dialog",
    "sysvar_organizer.ui.scan_worker",
    "sysvar_organizer.ui.pages.duplicates",
    "sysvar_organizer.ui.duplicate_worker",
    "sysvar_organizer.ui.pages.compare",
    "sysvar_organizer.ui.comparison_worker",
    "sysvar_organizer.ui.pages.photos",
    "sysvar_organizer.ui.visual_worker",
    "sysvar_organizer.ui.thumbnails",
    "sysvar_organizer.ui.pages.organization",
    "sysvar_organizer.ui.organization_worker",
    "sysvar_organizer.ui.pages.operations_review",
    "sysvar_organizer.ui.pages.history",
    "sysvar_organizer.ui.background",
    "sysvar_organizer.ui.pages.consolidation",
)


def is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False))


def run_self_check(
    app: QApplication,
    window: MainWindow,
    context: AppContext,
    exclusive: bool = True,
    dialog: bool = True,
) -> int:
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

    from sysvar_organizer.engines.visual_analysis.signatures import (
        PILLOW_VERSION,
        dependencies_error,
    )

    missing = dependencies_error()
    results.append(
        ("Bibliotecas de imagem (Pillow e numpy)", missing is None, missing or f"Pillow {PILLOW_VERSION}")
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

    results.append(_physical_operations_check(window, context))

    if context.database is None or context.history is None:
        results.append(_database_without_context(context))
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

    if context.database is not None:
        integrity = context.database.check_integrity(full=False)
        detail = (
            f"verificação rápida em {integrity.elapsed_seconds:.1f} s".replace(".", ",")
            if integrity.ok
            else "; ".join(integrity.problems[:3])
        )
        results.append(("Integridade do banco", integrity.ok, detail))
    notes = []
    if not exclusive:
        notes.append(
            "[AVISO] Outra instância do aplicativo está aberta: esta verificação não alterou "
            "nenhum estado."
        )

    window.close()
    app.processEvents()

    all_ok = all(ok for _, ok, _ in results)
    mode = "aplicativo instalado" if is_packaged() else "ambiente de desenvolvimento"
    lines = [
        f"{APP_NAME} {__version__} — verificação rápida ({mode})",
        f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}",
        *notes,
        *(f"[{'OK' if ok else 'FALHA'}] {name}: {detail}" for name, ok, detail in results),
        "Resultado: " + ("aprovado" if all_ok else "com falhas"),
    ]
    _publish(lines, all_ok, dialog)
    return 0 if all_ok else 1


def _physical_operations_check(window: MainWindow, context: AppContext) -> tuple[str, bool, str]:
    """Operações reais restritas a áreas autorizadas: sem plano controlado
    selecionado, os controles de execução física ficam desabilitados; a Lixeira
    está desabilitada; no aplicativo instalado, a área de testes não existe."""
    from sysvar_organizer.operations.recycle import RECYCLE_BIN_ENABLED

    organization = getattr(window, "_organization", None)
    review = getattr(organization, "_review", None)
    buttons = list(getattr(review, "_physical_buttons", []))
    plan = review._plan() if review is not None else None  # noqa: SLF001
    controlled = plan is not None and plan.area_id is not None
    gated = bool(buttons) and (controlled or not any(b.isEnabled() is True for b in buttons))
    areas = 0
    if context.operations is not None:
        try:
            areas = len(context.operations.areas())
        except DatabaseError:
            areas = 0
    test_area_absent = True
    if is_packaged():
        for name in TEST_ONLY_MODULES:
            try:
                importlib.import_module(name)
                test_area_absent = False
            except ImportError:
                pass
    ok = gated and not RECYCLE_BIN_ENABLED and test_area_absent
    if ok:
        detail = (
            f"execução física somente em áreas autorizadas ({areas} ativa(s)); "
            "Lixeira e exclusão desabilitadas"
        )
        if is_packaged():
            detail += "; área de testes ausente do pacote"
    else:
        detail = "ATENÇÃO: controles físicos sem autorização, Lixeira ativa ou área de testes no pacote"
    return ("Política de operações reais", ok, detail)


def _database_without_context(context: AppContext) -> tuple[str, bool, str]:
    category = context.database_error_category
    if category == "missing":
        # Ainda não há banco do usuário: valida o SQLite e as migrações num banco
        # temporário, sem criar nada na pasta de dados.
        try:
            with tempfile.TemporaryDirectory(prefix="sysvar-organizer-diagnostico-") as tmp:
                version = Database(Path(tmp) / "verificacao.db").initialize()
        except (DatabaseError, OSError) as exc:
            return ("Banco SQLite", False, f"falha no banco temporário: {exc}")
        return (
            "Banco SQLite",
            version == LATEST_SCHEMA_VERSION,
            "banco do usuário ainda não criado; SQLite verificado em banco temporário "
            f"(esquema v{version})",
        )
    if category == "outdated":
        return (
            "Banco SQLite",
            True,
            f"{context.database_path} (esquema v{context.schema_version}; atualização para "
            f"v{LATEST_SCHEMA_VERSION} pendente, aplicada na abertura normal)",
        )
    return ("Banco SQLite", False, context.database_error or "indisponível")


def _publish(lines: list[str], all_ok: bool, dialog: bool) -> None:
    text = "\n".join(lines)
    report: Path | None = paths.log_dir() / REPORT_FILE_NAME
    try:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text + "\n", encoding="utf-8")
    except OSError:
        report = None
    console = _console_stream()
    if console is not None:
        print(text, file=console, flush=True)
        return
    if dialog and is_packaged():
        where = f"\n\nRelatório gravado em:\n{report}" if report else ""
        show = QMessageBox.information if all_ok else QMessageBox.warning
        show(None, f"{APP_NAME} — diagnóstico", text + where)


def _console_stream():
    """Saída de console: a do processo ou, no executável sem console do
    Windows, o console de quem o iniciou (prompt de comando), se houver."""
    if sys.stdout is not None:
        return sys.stdout
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except (AttributeError, OSError):
        pass
    return None
