"""Exceções do aplicativo e captura de erros não tratados."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from types import TracebackType

logger = logging.getLogger(__name__)


class OrganizerError(Exception):
    """Erro base do Sysvar Organizer."""


class DatabaseError(OrganizerError):
    """Falha ao inicializar ou acessar o banco de dados local."""


ErrorNotifier = Callable[[str], None]


def install_exception_hook(notify: ErrorNotifier | None = None) -> None:
    """Registra erros não tratados no log e, se informado, avisa o usuário.

    ``notify`` recebe uma mensagem curta, sem detalhes técnicos; o rastreamento
    completo vai apenas para o log.
    """

    def handle(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        logger.critical("Erro não tratado", exc_info=(exc_type, exc_value, exc_tb))
        if notify is not None:
            try:
                notify(
                    "Ocorreu um erro inesperado. A operação foi interrompida e "
                    "os detalhes foram registrados no log do aplicativo."
                )
            except Exception:  # noqa: BLE001 - o aviso nunca deve derrubar o app
                logger.exception("Falha ao exibir aviso de erro")

    sys.excepthook = handle
