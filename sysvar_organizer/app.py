"""Inicialização da aplicação.

Uso:
    python -m sysvar_organizer                 inicia o aplicativo
    python -m sysvar_organizer --self-check    verificação rápida e encerra
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from sysvar_organizer import APP_NAME, ORGANIZATION_NAME, __version__
from sysvar_organizer.config import paths
from sysvar_organizer.context import build_context
from sysvar_organizer.core.errors import install_exception_hook
from sysvar_organizer.core.logging_setup import configure_logging
from sysvar_organizer.ui import theme
from sysvar_organizer.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

SELF_CHECK_FLAG = "--self-check"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    self_check = SELF_CHECK_FLAG in args

    try:
        paths.ensure_data_dirs()
    except OSError:
        pass  # o contexto registra o banco como indisponível
    configure_logging(paths.log_path())
    logger.info("Iniciando %s %s", APP_NAME, __version__)

    app = _create_application()
    install_exception_hook(_show_error_dialog)

    context = build_context()
    window = MainWindow(context)

    if self_check:
        from sysvar_organizer.diagnostics import run_self_check

        return run_self_check(app, window, context)

    window.show()
    if context.database_error:
        QMessageBox.warning(
            window,
            APP_NAME,
            "O banco de dados local não pôde ser preparado. O aplicativo "
            "funcionará sem persistência nesta sessão.\n\n" + context.database_error,
        )
    exit_code = app.exec()
    logger.info("Aplicativo encerrado (código %d)", exit_code)
    return exit_code


def _create_application() -> QApplication:
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName(ORGANIZATION_NAME)
    QApplication.setApplicationVersion(__version__)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    # Tema claro fixo: evita texto claro sobre fundo claro quando o Windows
    # está em modo escuro.
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):
        hints.setColorScheme(Qt.ColorScheme.Light)
    app.setPalette(app.style().standardPalette())
    app.setStyleSheet(theme.STYLESHEET)
    return app


def _show_error_dialog(message: str) -> None:
    if QApplication.instance() is not None:
        QMessageBox.critical(QApplication.activeWindow(), APP_NAME, message)


if __name__ == "__main__":
    raise SystemExit(main())
