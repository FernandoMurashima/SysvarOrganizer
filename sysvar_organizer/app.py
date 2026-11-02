"""Inicialização da aplicação.

Uso:
    python -m sysvar_organizer                 inicia o aplicativo
    python -m sysvar_organizer --self-check    verificação rápida e encerra

No aplicativo instalado, ``SysvarOrganizer.exe --self-check`` executa o mesmo
diagnóstico (somente leitura) e grava o resultado em ``logs/self-check.txt``.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QLockFile, Qt
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
NO_DIALOG_FLAG = "--no-dialog"
LOCK_FILE_NAME = "organizer.lock"
# Identificadores usados pelo Windows (agrupamento na barra de tarefas) e pelo
# instalador (detecção do aplicativo aberto antes de atualizar/desinstalar).
APP_USER_MODEL_ID = "Sysvar.Organizer"
APP_MUTEX_NAME = "SysvarOrganizer.AppMutex"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    self_check = SELF_CHECK_FLAG in args

    try:
        paths.ensure_data_dirs()
    except OSError:
        pass  # o contexto registra o banco como indisponível
    configure_logging(paths.log_path())
    logger.info("Iniciando %s %s", APP_NAME, __version__)

    if not self_check:
        _register_windows_identity()
    app = _create_application()
    install_exception_hook(_show_error_dialog)

    # Uma instância por vez: outra janela aberta ao mesmo tempo marcaria como
    # interrompidas as atividades em andamento da primeira.
    lock = QLockFile(str(paths.user_data_dir() / LOCK_FILE_NAME))
    lock.setStaleLockTime(0)  # obsoleto só se o processo dono não existir mais
    exclusive = bool(lock.tryLock(200))
    if not exclusive and not self_check:
        logger.warning("Outra instância do aplicativo já está aberta; encerrando")
        QMessageBox.information(
            None,
            APP_NAME,
            f"O {APP_NAME} já está aberto neste computador. Use a janela existente.",
        )
        return 1

    # O diagnóstico nunca cria, migra ou altera o banco do usuário.
    context = build_context(recover_interrupted=exclusive, read_only=self_check)
    window = MainWindow(context)

    if self_check:
        from sysvar_organizer.diagnostics import run_self_check

        try:
            return run_self_check(
                app, window, context, exclusive=exclusive, dialog=NO_DIALOG_FLAG not in args
            )
        finally:
            if exclusive:
                lock.unlock()

    window.show()
    if context.database_error:
        QMessageBox.warning(window, APP_NAME, database_error_text(context))
    exit_code = app.exec()
    lock.unlock()
    logger.info("Aplicativo encerrado (código %d)", exit_code)
    return exit_code


def database_error_text(context) -> str:
    advice = {
        "corrupted": "Não apague o arquivo: guarde uma cópia dele antes de qualquer tentativa "
        "de recuperação. Nenhuma reconstrução é feita automaticamente.",
        "incompatible": "Use a versão do aplicativo que criou este banco. O arquivo não foi "
        "modificado.",
        "inaccessible": "Verifique se outro programa está usando o arquivo e as permissões da "
        "pasta de dados.",
    }.get(context.database_error_category or "", "")
    return (
        "O banco de dados local não pôde ser preparado. O aplicativo funcionará sem "
        "persistência nesta sessão; o histórico existente não foi apagado.\n\n"
        f"{context.database_error}\n\n{advice}\n\nArquivo: {context.database_path}"
    ).strip()


_WINDOWS_HANDLES: list = []


def _register_windows_identity() -> None:
    """Identidade do processo no Windows; sem efeito em outros sistemas."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
        if handle:
            _WINDOWS_HANDLES.append(handle)  # mantido aberto até o fim do processo
    except (AttributeError, OSError):
        logger.warning("Não foi possível registrar a identidade do aplicativo no Windows")


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
