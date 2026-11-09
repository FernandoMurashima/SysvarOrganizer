"""Envio à Lixeira do Windows — SEGUNDA FASE, DESABILITADO.

Arquitetura preparada para uma etapa futura, que só será ativada após a
aprovação dos testes de cópia, movimentação e reversão e de autorização
específica do proprietário. Exclusão PERMANENTE não existe e não será criada.

Contrato previsto (quando autorizado):

* enviar o arquivo à Lixeira (``IFileOperation`` com ``FOFX_RECYCLEONDELETE``
  ou ``SHFileOperationW`` com ``FOF_ALLOWUNDO``), nunca apagar definitivamente;
* recusar quando a unidade não oferecer Lixeira (rede, mídia removível,
  Lixeira desativada ou arquivo maior que o limite da Lixeira) — nesses
  casos o Windows apagaria de forma permanente;
* exigir confirmação específica, registrar cada arquivo e permitir
  verificar a recuperação (presença na Lixeira) após a operação;
* valer somente dentro de áreas autorizadas, com as mesmas validações de
  caminho e identidade das demais operações.

Nesta fase todas as funções recusam a operação.
"""

from __future__ import annotations

RECYCLE_BIN_ENABLED = False
DISABLED_MESSAGE = (
    "O envio à Lixeira está desabilitado nesta fase de homologação. Ele só será ativado "
    "após a aprovação dos testes de cópia, movimentação e reversão."
)


class RecycleRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def recycle_capability(path: str) -> tuple[bool, str]:
    """A Lixeira pode garantir a recuperação deste arquivo? Nesta fase: não."""
    return False, DISABLED_MESSAGE


def send_to_recycle_bin(path: str) -> None:
    """Sempre recusa nesta fase (nenhum arquivo é tocado)."""
    raise RecycleRefused("recycle_disabled", DISABLED_MESSAGE)
