"""Executor seguro de operações físicas: copiar, mover, reverter e reconciliar.

Autorização (verificada pelo próprio executor, independentemente da interface):

* Modo CONTROLADO (``SafeExecutor(store, None)``): o plano precisa ser do
  ambiente real, ter sido criado vinculado a áreas autorizadas ATIVAS e cuja
  identidade física confere (``authorization``). Origem e destino de cada
  ação precisam estar dentro dessas áreas. Planos sem área (inclusive todos
  os anteriores ao Bloco 10) são sempre recusados.
* Modo TESTE (``SafeExecutor(store, area_de_teste)``): só para os testes
  automatizados, com uma ``IsolatedTestArea`` válida; recusado no aplicativo
  distribuído.

Garantias (iguais nos dois modos):

* Estados explícitos e trocas por comparação (planejada → validada → em
  execução → concluída/falhou; concluída → reversão pendente → revertida).
  O estado "em execução" é gravado ANTES de tocar no disco; uma nova
  chamada sobre uma ação concluída não faz nada (sem execução duplicada).
* Validação imediata antes de abrir ou alterar arquivos: caminho dentro da
  área, sem links/junções em nenhum componente, origem existente, comum e
  idêntica à validada (tamanho, data em ns, identificador físico), destino
  livre, permissões e espaço.
* Cópia: grava em arquivo temporário exclusivo na pasta de destino, confere
  o SHA-256 com o da origem validada e só então cria o nome final sem
  sobrescrita (link do temporário; no Windows, renomeação que falha se o
  destino existir). A origem é preservada.
* Movimentação no mesmo volume: cria o nome de destino como link do mesmo
  arquivo físico (falha se o destino existir), confere que é o mesmo arquivo
  e só então remove o nome de origem. Entre volumes (ou sem suporte a link):
  cópia verificada e, só depois de conferir o SHA-256 do destino, remoção da
  origem. Nunca há momento em que a única cópia válida deixe de existir.
* Falhas e cancelamento: o temporário (criado pelo próprio executor) é
  removido; a origem é preservada; o resultado é registrado. Falta de espaço
  e falta de permissão são relatadas como bloqueios (nada foi alterado).
* Exclusão de arquivos do usuário não existe: o único arquivo removido é o
  nome de origem de uma movimentação já conferida no destino.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
import sys
import threading
from collections.abc import Callable

from sysvar_organizer.operations.models import (
    ACTION_COPY,
    ACTION_KEEP,
    ACTION_MOVE,
    ACTION_REVIEW,
    ENV_REAL,
    ENV_TEST,
    REAL_OPERATIONS_BLOCKED_MESSAGE,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INTERRUPTED,
    STATE_PLANNED,
    STATE_REVERT_PENDING,
    STATE_REVERTED,
    STATE_RUNNING,
    STATE_VALIDATED,
)
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.operations.authorization import AuthorizedArea
from sysvar_organizer.operations.safety import ConfinedRoot, UnsafePath, is_link_like
from sysvar_organizer.persistence.operations_store import (
    OperationItem,
    OperationsStore,
    PlanRecord,
)

logger = logging.getLogger(__name__)

CHUNK = 1024 * 1024
SPACE_MARGIN = 1024 * 1024
MOVE_ACTIONS = (ACTION_MOVE, ACTION_REVIEW)


class OperationRefused(Exception):
    """A operação não pode ser feita com segurança (nada foi alterado)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OperationCancelled(Exception):
    """Cancelamento solicitado durante a gravação (temporário descartado)."""


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _regular(path: str) -> os.stat_result:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise OperationRefused("source_missing", "O arquivo não existe mais.") from exc
    if is_link_like(st):
        raise OperationRefused("link", "Links simbólicos e junções não são seguidos.")
    if not stat.S_ISREG(st.st_mode):
        raise OperationRefused("not_regular", "Não é um arquivo comum.")
    return st


def _refusal_from_os_error(exc: OSError) -> OperationRefused | None:
    """Erros do sistema que significam "nada foi feito" viram bloqueio explicado."""
    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or getattr(exc, "winerror", 0) in (39, 112):
        return OperationRefused("no_space", "Espaço insuficiente no destino; nada foi alterado.")
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return OperationRefused("permission", "Sem permissão para concluir a operação; nada foi alterado.")
    return None


class SafeExecutor:
    def __init__(
        self,
        store: OperationsStore,
        area=None,
        fault: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """``area=None``: modo controlado (áreas autorizadas no banco).
        ``area=IsolatedTestArea``: modo de teste automatizado."""
        self._store = store
        self._fault = fault or (lambda _checkpoint: None)
        self._cancel = cancel
        self._created: tuple[str, tuple[int, int]] | None = None
        self._test_area = None
        if area is not None:
            if getattr(sys, "frozen", False):
                # Áreas de teste não existem no aplicativo distribuído.
                raise OperationRefused("packaged_build", "Área de testes recusada no aplicativo distribuído.")
            # Reabre a área para conferir marcador e token antes de qualquer uso.
            self._test_area = type(area).open(area.root, area.token)

    @property
    def controlled(self) -> bool:
        return self._test_area is None

    # Autorização -----------------------------------------------------------

    def _plan(self, item: OperationItem) -> PlanRecord:
        plan = self._store.plan(item.plan_id)
        if plan is None:
            raise OperationRefused("plan_missing", "Plano inexistente.")
        return plan

    def _area(self, area_id: int | None) -> AuthorizedArea:
        record = self._store.area(area_id) if area_id is not None else None
        if record is None:
            raise OperationRefused("real_operations_blocked", REAL_OPERATIONS_BLOCKED_MESSAGE)
        area = AuthorizedArea(
            record.id, record.root, record.volume_id, record.dir_index, record.status
        )
        try:
            return area.verify()
        except UnsafePath as exc:
            raise OperationRefused(exc.code, exc.message) from exc

    def _scope(self, item: OperationItem) -> tuple[ConfinedRoot, ConfinedRoot]:
        """Raízes autorizadas para a origem e o destino da ação."""
        plan = self._plan(item)
        if self._test_area is not None:
            if plan.environment != ENV_TEST or not plan.test_root:
                raise OperationRefused(
                    "wrong_scope", "O executor de testes só aceita planos da área de testes."
                )
            if os.path.normcase(os.path.normpath(plan.test_root)) != os.path.normcase(
                self._test_area.root
            ):
                raise OperationRefused(
                    "outside_area", "O plano não pertence a esta área de testes isolada."
                )
            return self._test_area, self._test_area
        if plan.environment != ENV_REAL or plan.area_id is None:
            raise OperationRefused("real_operations_blocked", REAL_OPERATIONS_BLOCKED_MESSAGE)
        source_area = self._area(plan.area_id)
        destination_id = plan.destination_area_id or plan.area_id
        destination_area = (
            source_area if destination_id == source_area.id else self._area(destination_id)
        )
        return source_area, destination_area

    def _load(self, item_id: int) -> OperationItem:
        item = self._store.item(item_id)
        if item is None:
            raise OperationRefused("item_missing", "Ação inexistente.")
        self._scope(item)
        return item

    def _paths(self, item: OperationItem) -> tuple[str, str | None]:
        source_root, destination_root = self._scope(item)
        source = source_root.resolve(item.source_path)
        target = destination_root.resolve(item.destination_path) if item.destination_path else None
        return source, target

    def _where(self) -> str:
        return "na área autorizada" if self.controlled else "na área de testes"

    def _block(self, item: OperationItem, from_states, code: str, message: str) -> str:
        self._store.transition(item.id, from_states, STATE_BLOCKED, block_reason=message)
        self._store.add_event(item.plan_id, item.id, "warning", code, message)
        return STATE_BLOCKED

    # Validação ---------------------------------------------------------------

    def validate(self, item_id: int) -> str:
        """Revalida a ação aprovada e registra a identidade física da origem."""
        item = self._load(item_id)
        plan = self._store.plan(item.plan_id)
        if plan.status != "open":
            raise OperationRefused("plan_closed", "O planejamento foi cancelado.")
        if item.state not in (STATE_PLANNED, STATE_VALIDATED):
            raise OperationRefused("invalid_state", f"Estado atual não permite validar: {item.state}.")
        if not item.approved:
            raise OperationRefused("not_approved", "A ação não foi selecionada pelo usuário.")
        active = (STATE_PLANNED, STATE_VALIDATED)

        for member in self._store.unit_members(item.plan_id, item.unit_key):
            if member.state not in (*active, STATE_COMPLETED) or not member.approved:
                return self._block(
                    item, active, "dependency",
                    "Arquivo associado não aprovado ou bloqueado: a unidade deve seguir junta.",
                )
        if item.sensitive and item.action_type != ACTION_KEEP:
            return self._block(item, active, "sensitive", "Estrutura sensível: movimentação parcial proibida.")

        try:
            source, target = self._paths(item)
            st = _regular(source)
        except UnsafePath as exc:
            return self._block(item, active, exc.code, exc.message)
        except OperationRefused as exc:
            return self._block(item, active, exc.code, exc.message)

        if st.st_size != item.size_bytes or (
            item.expected_mtime is not None and int(st.st_mtime) != item.expected_mtime
        ):
            return self._block(item, active, "modified", "Arquivo modificado depois do planejamento.")
        if not os.access(source, os.R_OK):
            return self._block(item, active, "permission", "Sem permissão de leitura na origem.")

        if item.action_type != ACTION_KEEP:
            if target is None:
                return self._block(item, active, "no_destination", "Ação sem destino.")
            if os.path.lexists(target):
                return self._block(item, active, "destination_occupied", "O destino já está ocupado.")
            if item.action_type in MOVE_ACTIONS and not os.access(os.path.dirname(source), os.W_OK):
                return self._block(item, active, "permission", "Sem permissão para retirar o arquivo da origem.")
            ancestor = self._existing_ancestor(target)
            if not os.access(ancestor, os.W_OK):
                return self._block(item, active, "permission", "Sem permissão de escrita no destino.")
            if shutil.disk_usage(ancestor).free < st.st_size + SPACE_MARGIN:
                return self._block(item, active, "no_space", "Espaço livre insuficiente no destino.")

        digest = sha256_of(source)
        after = os.lstat(source)
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return self._block(item, active, "changed_during_validation", "Arquivo alterado durante a validação.")
        self._store.transition(
            item.id, active, STATE_VALIDATED,
            src_volume=str(st.st_dev), src_index=str(st.st_ino),
            src_mtime_ns=st.st_mtime_ns, sha256=digest, block_reason=None,
        )
        self._store.add_event(item.plan_id, item.id, "info", "validated", f"Ação validada {self._where()}.")
        return STATE_VALIDATED

    # Execução ----------------------------------------------------------------

    def execute(self, item_id: int) -> str:
        item = self._load(item_id)
        if item.state == STATE_COMPLETED:
            self._store.add_event(
                item.plan_id, item.id, "warning", "duplicate_request",
                "Pedido repetido ignorado: a ação já foi concluída.",
            )
            return STATE_COMPLETED
        if item.state != STATE_VALIDATED or not item.approved:
            raise OperationRefused("invalid_state", "Somente ações aprovadas e validadas podem ser executadas.")

        execution = self._store.start_execution(item.id, "execute", item.state)
        if not self._store.transition(item.id, (STATE_VALIDATED,), STATE_RUNNING):
            self._store.finish_execution(execution, "blocked", None, "Estado mudou antes da execução.")
            return self._store.item(item.id).state
        self._store.add_event(item.plan_id, item.id, "info", "running", f"Execução iniciada {self._where()}.")

        temp = source = None
        self._created = None
        try:
            source, target = self._paths(item)
            st = _regular(source)
            if (str(st.st_dev), str(st.st_ino), st.st_size, st.st_mtime_ns) != (
                item.src_volume, item.src_index, item.size_bytes, item.src_mtime_ns,
            ):
                raise OperationRefused("modified", "Arquivo modificado depois da validação.")

            if item.action_type == ACTION_KEEP:
                method = "nenhuma (mantido no local; existência conferida)"
                final = st
            else:
                if os.path.lexists(target):
                    raise OperationRefused("destination_occupied", "O destino foi ocupado depois da validação.")
                self._make_parents(target, self._scope(item)[1])
                temp = self._temp_name(item, target)
                if item.action_type == ACTION_COPY:
                    method = self._copy(source, target, temp, item)
                else:
                    method = self._move(source, target, temp, item, st)
                temp = None
                final = _regular(target)
                if sha256_of(target) != item.sha256:
                    raise RuntimeError("Conferência do destino falhou (SHA-256 diferente).")
                if item.action_type in MOVE_ACTIONS and os.path.lexists(source):
                    raise RuntimeError("A origem ainda existe após a movimentação.")

            self._store.transition(
                item.id, (STATE_RUNNING,), STATE_COMPLETED,
                dst_volume=str(final.st_dev), dst_index=str(final.st_ino),
                dst_mtime_ns=final.st_mtime_ns,
                reversible=int(item.action_type in MOVE_ACTIONS),
            )
            self._store.finish_execution(execution, STATE_COMPLETED, method, "Concluída e conferida.")
            self._store.add_event(item.plan_id, item.id, "info", "completed", f"Concluída: {method}.")
            return STATE_COMPLETED
        except DatabaseError:
            # O registro falhou, não o disco: o estado físico fica como está e a
            # ação permanece "em execução" para ser reconciliada na próxima abertura.
            self._cleanup_temp(temp)
            self._created = None
            raise
        except OperationCancelled:
            note = self._abort(temp, source, item)
            message = "Cancelada antes da conclusão; temporário descartado e origem preservada." + note
            self._store.transition(item.id, (STATE_RUNNING,), STATE_VALIDATED)
            self._store.finish_execution(execution, "cancelled", None, message)
            self._store.add_event(item.plan_id, item.id, "warning", "cancelled", message)
            return STATE_VALIDATED
        except (OSError, OperationRefused, UnsafePath) as exc:
            note = self._abort(temp, source, item)
            refused = exc if not isinstance(exc, OSError) else _refusal_from_os_error(exc)
            if refused is None:
                return self._execution_failed(item, execution, exc, note)
            message = refused.message + note
            self._store.transition(item.id, (STATE_RUNNING,), STATE_BLOCKED, block_reason=message)
            self._store.finish_execution(execution, STATE_BLOCKED, None, message)
            self._store.add_event(item.plan_id, item.id, "warning", refused.code, message)
            return STATE_BLOCKED
        except Exception as exc:  # noqa: BLE001 - qualquer falha física é registrada
            note = self._abort(temp, source, item)
            return self._execution_failed(item, execution, exc, note)

    def _abort(
        self, temp: str | None, source: str | None, item: OperationItem, reverting: bool = False
    ) -> str:
        """Descarta o temporário e, se a origem continua intacta, desfaz o destino
        criado NESTA execução (nunca toca em arquivo que não foi criado aqui)."""
        self._cleanup_temp(temp)
        created, self._created = self._created, None
        if created is None or source is None:
            return ""
        target, identity = created
        try:
            src = os.lstat(source)
            dst = os.lstat(target)
        except OSError:
            return ""  # sem origem: o destino é a única cópia e é preservado
        expected = (
            (item.dst_volume, item.dst_index, item.size_bytes)
            if reverting
            else (item.src_volume, item.src_index, item.size_bytes)
        )
        if (str(src.st_dev), str(src.st_ino), src.st_size) != expected or (
            dst.st_dev, dst.st_ino
        ) != identity:
            return " Destino preservado para revisão manual."
        try:
            os.unlink(target)
        except OSError:
            logger.error("Não foi possível desfazer o destino criado (ação=%s)", item.id)
            return " Não foi possível desfazer o arquivo criado; revisão manual necessária."
        return " O arquivo criado nesta execução foi desfeito; o original permanece intacto."

    def _execution_failed(
        self, item: OperationItem, execution: int, exc: BaseException, note: str = ""
    ) -> str:
        message = f"Falha durante a execução: {exc}. A origem foi preservada.{note}"
        logger.error("Falha na execução (ação=%s, tipo=%s)", item.id, type(exc).__name__)
        self._store.transition(item.id, (STATE_RUNNING,), STATE_FAILED, block_reason=message)
        self._store.finish_execution(execution, STATE_FAILED, None, message)
        self._store.add_event(item.plan_id, item.id, "error", "failed", message)
        return STATE_FAILED

    # Reversão ----------------------------------------------------------------

    def revert_preflight(self, item_id: int) -> str | None:
        """Confere, SEM alterar nada, se a reversão é possível. Retorna o motivo
        do bloqueio ou ``None``."""
        try:
            item = self._load(item_id)
            self._check_revertible(item)
            original, current = self._paths(item)
            st = _regular(current)
        except (OperationRefused, UnsafePath) as exc:
            return exc.message
        if (str(st.st_dev), str(st.st_ino), st.st_size, st.st_mtime_ns) != (
            item.dst_volume, item.dst_index, item.size_bytes, item.dst_mtime_ns,
        ) or sha256_of(current) != item.sha256:
            return "O arquivo foi alterado depois da operação; reversão bloqueada."
        if os.path.lexists(original):
            return "O caminho original está ocupado; a reversão não sobrescreve arquivos."
        return None

    @staticmethod
    def _check_revertible(item: OperationItem) -> None:
        if item.state != STATE_COMPLETED or not item.reversible:
            message = (
                "Cópia não é revertida por remoção: a origem foi preservada e a cópia permanece."
                if item.action_type == ACTION_COPY
                else "Somente movimentações concluídas e conferidas podem ser revertidas."
            )
            raise OperationRefused("not_reversible", message)

    def revert(self, item_id: int) -> str:
        item = self._load(item_id)
        self._check_revertible(item)
        recovery = self._store.start_recovery(item.id)
        if not self._store.transition(item.id, (STATE_COMPLETED,), STATE_REVERT_PENDING):
            self._store.finish_recovery(recovery, "blocked", "Estado mudou antes da reversão.")
            return self._store.item(item.id).state
        temp = current = None
        self._created = None
        try:
            original, current = self._paths(item)
            st = _regular(current)
            if (str(st.st_dev), str(st.st_ino), st.st_size, st.st_mtime_ns) != (
                item.dst_volume, item.dst_index, item.size_bytes, item.dst_mtime_ns,
            ) or sha256_of(current) != item.sha256:
                raise OperationRefused(
                    "modified_after_operation",
                    "O arquivo foi alterado depois da operação; reversão bloqueada.",
                )
            if os.path.lexists(original):
                raise OperationRefused(
                    "original_occupied",
                    "O caminho original está ocupado; a reversão não sobrescreve arquivos.",
                )
            self._make_parents(original, self._scope(item)[0])
            temp = self._temp_name(item, original)
            method = self._move(current, original, temp, item, st)
            temp = None
            _regular(original)
            if sha256_of(original) != item.sha256 or os.path.lexists(current):
                raise RuntimeError("Conferência da reversão falhou.")
            self._store.transition(item.id, (STATE_REVERT_PENDING,), STATE_REVERTED, reversible=0)
            self._store.finish_recovery(recovery, "reverted", f"Revertida: {method}.")
            self._store.add_event(item.plan_id, item.id, "info", "reverted", "Movimentação revertida e conferida.")
            return STATE_REVERTED
        except DatabaseError:
            self._cleanup_temp(temp)
            self._created = None
            raise
        except (OSError, OperationRefused, UnsafePath) as exc:
            note = self._abort(temp, current, item, reverting=True)
            refused = exc if not isinstance(exc, OSError) else _refusal_from_os_error(exc)
            if refused is None:
                return self._revert_failed(item, recovery, exc, note)
            message = refused.message + note
            self._store.transition(item.id, (STATE_REVERT_PENDING,), STATE_COMPLETED)
            self._store.finish_recovery(recovery, "blocked", message)
            self._store.add_event(item.plan_id, item.id, "warning", refused.code, message)
            return STATE_COMPLETED
        except Exception as exc:  # noqa: BLE001
            note = self._abort(temp, current, item, reverting=True)
            return self._revert_failed(item, recovery, exc, note)

    def _revert_failed(self, item: OperationItem, recovery: int, exc: BaseException, note: str) -> str:
        message = f"Falha na reversão: {exc}. Nenhum arquivo foi removido sem cópia conferida.{note}"
        logger.error("Falha na reversão (ação=%s, tipo=%s)", item.id, type(exc).__name__)
        self._store.transition(item.id, (STATE_REVERT_PENDING,), STATE_FAILED, block_reason=message)
        self._store.finish_recovery(recovery, "failed", message)
        self._store.add_event(item.plan_id, item.id, "error", "revert_failed", message)
        return STATE_FAILED

    # Reconciliação após interrupção ------------------------------------------

    def reconcile(self, item_id: int) -> str:
        """Examina o estado físico de uma ação interrompida e registra o resultado."""
        item = self._load(item_id)
        if item.state != STATE_INTERRUPTED:
            raise OperationRefused("invalid_state", "Somente ações interrompidas são reconciliadas.")
        execution = self._store.start_execution(item.id, "reconcile", item.state)
        source, target = self._paths(item)
        source_root, destination_root = self._scope(item)
        removed = self._remove_own_temps(item, target, destination_root) + self._remove_own_temps(
            item, source, source_root
        )
        src_ok = self._matches(source, item.sha256)
        dst_ok = self._matches(target, item.sha256) if target else False
        was_reverting = item.dst_index is not None  # já tinha sido concluída antes

        twin = self._same_physical(source, target)
        if item.action_type == ACTION_COPY:
            if src_ok and dst_ok:
                state, note = STATE_COMPLETED, "Cópia encontrada íntegra no destino."
            else:
                state, note = STATE_FAILED, "Cópia não concluída; a origem foi preservada."
        elif twin and src_ok:
            # Interrompida logo após criar o segundo nome do MESMO arquivo físico.
            # Regra homologada (Bloco 07): a reconciliação não remove nada.
            state, note = (
                STATE_FAILED,
                "Interrompida: origem e destino são dois nomes do MESMO arquivo físico (nenhum "
                "espaço extra; nenhum conteúdo perdido). Nada foi removido; revisão manual: "
                "mantenha apenas um dos nomes.",
            )
        elif was_reverting:
            if src_ok and not os.path.lexists(target):
                state, note = STATE_REVERTED, "Reversão encontrada concluída e íntegra."
            elif dst_ok and not os.path.lexists(source):
                state, note = STATE_COMPLETED, "Reversão não ocorreu; arquivo segue no destino."
            else:
                state, note = STATE_FAILED, "Estado ambíguo após interrupção; revisão manual necessária."
        else:
            if dst_ok and not os.path.lexists(source):
                state, note = STATE_COMPLETED, "Movimentação encontrada concluída e íntegra."
            elif src_ok and not (target and os.path.lexists(target)):
                state, note = STATE_FAILED, "Movimentação não ocorreu; a origem foi preservada."
            else:
                state, note = (
                    STATE_FAILED,
                    "Origem e destino existem após interrupção; nenhum arquivo foi removido. "
                    "Revisão manual necessária.",
                )
        values = {"block_reason": note if state == STATE_FAILED else None}
        if state == STATE_COMPLETED and item.action_type != ACTION_COPY and target:
            final = os.lstat(target)
            values.update(
                dst_volume=str(final.st_dev), dst_index=str(final.st_ino),
                dst_mtime_ns=final.st_mtime_ns, reversible=1,
            )
        if state == STATE_REVERTED:
            values["reversible"] = 0
        if removed:
            note += f" {removed} arquivo(s) temporário(s) do próprio executor removido(s)."
        self._store.transition(item.id, (STATE_INTERRUPTED,), state, **values)
        self._store.finish_execution(execution, state, "reconciliação", note)
        self._store.add_event(item.plan_id, item.id, "info" if state != STATE_FAILED else "warning",
                              "reconciled", note)
        return state

    # Operações físicas ---------------------------------------------------------

    def _copy(self, source: str, target: str, temp: str, item: OperationItem) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temp, flags, 0o600)
        digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as out, open(source, "rb", buffering=0) as src:
                while True:
                    if self._cancel is not None and self._cancel.is_set():
                        raise OperationCancelled()
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            self._fault("after_temp_write")
            if digest.hexdigest() != item.sha256:
                raise OperationRefused("modified", "A origem mudou durante a cópia.")
            shutil.copystat(source, temp)
            self._finalize_without_overwrite(temp, target)
            created = os.lstat(target)
            self._created = (target, (created.st_dev, created.st_ino))
        except BaseException:
            self._cleanup_temp(temp)
            raise
        return "cópia em temporário + conferência SHA-256 + nome final sem sobrescrita"

    def _move(self, source: str, target: str, temp: str, item: OperationItem, st) -> str:
        same_volume = os.lstat(os.path.dirname(target)).st_dev == st.st_dev
        if same_volume:
            try:
                os.link(source, target)
            except FileExistsError as exc:
                raise OperationRefused("destination_occupied", "O destino foi ocupado.") from exc
            except OSError:
                same_volume = False  # sistema sem suporte a links: usa cópia verificada
            else:
                linked = os.lstat(target)
                self._created = (target, (linked.st_dev, linked.st_ino))
                self._fault("after_link")
                linked, original = os.lstat(target), os.lstat(source)
                if (linked.st_dev, linked.st_ino) != (original.st_dev, original.st_ino):
                    raise RuntimeError("O destino não corresponde ao arquivo de origem.")
                os.unlink(source)
                return "novo nome no mesmo volume (link) + remoção do nome de origem"
        self._copy(source, target, temp, item)
        self._fault("after_copy_before_remove")
        if sha256_of(target) != item.sha256:
            raise RuntimeError("Cópia entre volumes não conferiu; a origem foi preservada.")
        current = os.lstat(source)
        if (current.st_size, current.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise RuntimeError("A origem mudou durante a movimentação; a origem foi preservada.")
        os.unlink(source)
        return "cópia verificada entre volumes + remoção da origem somente após conferência"

    @staticmethod
    def _finalize_without_overwrite(temp: str, target: str) -> None:
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise OperationRefused("destination_occupied", "O destino foi ocupado.") from exc
        except OSError:
            if os.name == "nt":
                os.rename(temp, target)  # no Windows, falha se o destino existir
                return
            if os.path.lexists(target):
                raise OperationRefused("destination_occupied", "O destino foi ocupado.") from None
            os.rename(temp, target)
            return
        os.unlink(temp)

    # Auxiliares ----------------------------------------------------------------

    def _make_parents(self, target: str, root: ConfinedRoot) -> None:
        missing = []
        current = os.path.dirname(target)
        while not os.path.lexists(current):
            missing.append(current)
            current = os.path.dirname(current)
        for folder in reversed(missing):
            root.resolve(folder)
            os.mkdir(folder)
        root.resolve(target)  # confere de novo após criar as pastas

    def _existing_ancestor(self, target: str) -> str:
        current = os.path.dirname(target)
        while not os.path.lexists(current):
            current = os.path.dirname(current)
        return current

    @staticmethod
    def _temp_name(item: OperationItem, target: str) -> str:
        return os.path.join(
            os.path.dirname(target),
            f".{os.path.basename(target)}.sysvar-tmp-{item.action_key[:12]}",
        )

    def _remove_own_temps(self, item: OperationItem, path: str | None, root: ConfinedRoot) -> int:
        """Remove somente o temporário que o próprio executor cria para esta ação."""
        if not path:
            return 0
        temp = self._temp_name(item, path)
        if os.path.lexists(temp):
            root.resolve(temp)
            os.unlink(temp)
            return 1
        return 0

    @staticmethod
    def _cleanup_temp(temp: str | None) -> None:
        if temp and os.path.lexists(temp):
            try:
                os.unlink(temp)
            except OSError:
                logger.exception("Não foi possível remover o temporário do executor")

    @staticmethod
    def _same_physical(first: str | None, second: str | None) -> bool:
        if not first or not second:
            return False
        try:
            a, b = os.lstat(first), os.lstat(second)
        except OSError:
            return False
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) and stat.S_ISREG(a.st_mode)

    @staticmethod
    def _matches(path: str | None, digest: str | None) -> bool:
        if not path or not digest:
            return False
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and not is_link_like(st) and sha256_of(path) == digest
