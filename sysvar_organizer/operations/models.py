"""Tipos de ação, estados e rótulos das operações."""

from __future__ import annotations

ENV_REAL = "real"
ENV_TEST = "test"

PLAN_OPEN = "open"
PLAN_CANCELLED = "cancelled"

ACTION_KEEP = "keep"
ACTION_COPY = "copy"
ACTION_MOVE = "move"
ACTION_REVIEW = "separate_review"
ACTION_RESTORE = "restore"
PHYSICAL_ACTIONS = (ACTION_COPY, ACTION_MOVE, ACTION_REVIEW)

STATE_PLANNED = "planned"
STATE_VALIDATED = "validated"
STATE_BLOCKED = "blocked"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_REVERT_PENDING = "revert_pending"
STATE_REVERTED = "reverted"
STATE_INTERRUPTED = "interrupted"

ACTION_LABELS = {
    ACTION_KEEP: "Manter no local",
    ACTION_COPY: "Copiar para destino",
    ACTION_MOVE: "Mover para destino",
    ACTION_REVIEW: "Separar para revisão",
    ACTION_RESTORE: "Restaurar movimentação",
}

STATE_LABELS = {
    STATE_PLANNED: "Planejada",
    STATE_VALIDATED: "Validada",
    STATE_BLOCKED: "Bloqueada",
    STATE_RUNNING: "Em execução",
    STATE_COMPLETED: "Concluída",
    STATE_FAILED: "Falhou",
    STATE_REVERT_PENDING: "Reversão pendente",
    STATE_REVERTED: "Revertida",
    STATE_INTERRUPTED: "Interrompida — requer reconciliação",
}

REAL_OPERATIONS_BLOCKED_MESSAGE = (
    "Operação real não autorizada: este plano não está vinculado a uma área de homologação "
    "autorizada. Ele pode ser revisado, mas nunca executado. Para executar, autorize uma "
    "pasta de homologação e crie um novo plano a partir de uma prévia dessa pasta."
)
CONTROLLED_OPERATIONS_MESSAGE = (
    "Teste real controlado: copiar, mover e reverter SOMENTE dentro das áreas autorizadas, "
    "após seleção individual, validação no disco e confirmação final. Exclusão e envio à "
    "Lixeira continuam bloqueados."
)

MODE_SIMULATION = "SIMULAÇÃO"
MODE_CONTROLLED = "TESTE REAL CONTROLADO"
MODE_UNAUTHORIZED = "OPERAÇÃO REAL NÃO AUTORIZADA"
MODE_AUTOMATED_TEST = "TESTE AUTOMATIZADO (área isolada)"


def plan_mode(environment: str, area_id: int | None) -> str:
    """Modo de um plano: controlado (área autorizada), não autorizado ou teste."""
    if environment == ENV_TEST:
        return MODE_AUTOMATED_TEST
    return MODE_CONTROLLED if area_id is not None else MODE_UNAUTHORIZED


def display_state(state: str, environment: str, area_id: int | None = None) -> str:
    """Rótulo que distingue simulação, bloqueio, teste, conclusão, falha e reversão."""
    if environment == ENV_REAL and area_id is None and state in (STATE_PLANNED, STATE_VALIDATED):
        return "Simulação (execução real bloqueada)"
    if environment == ENV_REAL and area_id is not None:
        if state == STATE_PLANNED:
            return "Planejada (aguarda validação)"
        if state == STATE_VALIDATED:
            return "Validada no disco"
        return STATE_LABELS.get(state, state)
    if environment == ENV_TEST and state == STATE_VALIDATED:
        return "Autorizada em ambiente de teste"
    label = STATE_LABELS.get(state, state)
    return f"{label} (teste)" if environment == ENV_TEST else label
