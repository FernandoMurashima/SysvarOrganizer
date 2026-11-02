"""Ponto de entrada do aplicativo empacotado (executável do Windows).

Garante uma mensagem compreensível mesmo quando a falha ocorre antes de a
interface existir (por exemplo, biblioteca ausente no pacote): o detalhe
técnico vai para ``logs/startup-error.log`` e o usuário recebe um aviso curto.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime

STARTUP_LOG = "startup-error.log"


def run(argv: list[str] | None = None) -> int:
    try:
        from sysvar_organizer.app import main
    except Exception:  # noqa: BLE001 - qualquer falha de carregamento vira aviso
        return _fatal("Não foi possível carregar os componentes do aplicativo.")
    try:
        return main(argv)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - erro antes do tratamento da interface
        return _fatal("Ocorreu um erro inesperado durante a inicialização.")


def _fatal(message: str) -> int:
    detail = traceback.format_exc()
    where = _write_log(detail)
    text = f"{message}\n\nOs detalhes foram registrados em:\n{where}" if where else message
    _show(text)
    return 1


def _write_log(detail: str) -> str | None:
    try:
        from sysvar_organizer.config import paths

        folder = paths.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / STARTUP_LOG
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n{detail}\n")
        return str(target)
    except Exception:  # noqa: BLE001 - o aviso ao usuário não pode falhar
        return None


def _show(text: str) -> None:
    title = "Sysvar Organizer"
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
            return
        except Exception:  # noqa: BLE001
            pass
    if sys.stderr is not None:
        print(f"{title}: {text}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(run())
