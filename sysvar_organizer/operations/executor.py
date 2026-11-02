"""Executor seguro de operações — SOMENTE dentro de uma área de testes isolada.

Arquitetura:

* O executor exige uma ``IsolatedTestArea`` válida (marcador + token) e só
  aceita planos do ambiente "test" cuja raiz seja exatamente essa área.
  Planos do ambiente real são sempre recusados: nesta etapa, operações reais
  estão bloqueadas.
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
  destino existir).
* Movimentação no mesmo volume: cria o nome de destino como link do mesmo
  arquivo físico (falha se o destino existir), confere que é o mesmo arquivo
  e só então remove o nome de origem. Entre volumes (ou sem suporte a link):
  cópia verificada e, só depois de conferir o SHA-256 do destino, remoção da
  origem. Nunca há momento em que a única cópia válida deixe de existir.
* Falhas: o temporário (criado pelo próprio executor) é removido; a origem é
  preservada; o resultado é registrado. Sucesso só é registrado depois de
  conferir o estado físico do destino.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import sys
from collections.abc import Callable

from sysvar_organizer.operations.models import (
    ACTION_COPY,
    ACTION_KEEP,
    ACTION_MOVE,
    ACTION_REVIEW,
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
from sysvar_organizer.operations.safety import IsolatedTestArea, UnsafePath, is_link_like
from sysvar_organizer.persistence.operations_store import OperationItem, OperationsStore

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


class SafeExecutor:
    def __init__(
        self,
        store: OperationsStore,
        area: IsolatedTestArea,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if getattr(sys, "frozen", False):
            # O aplicativo distribuído (empacotado) nunca executa operações físicas;
            # este módulo nem é incluído no pacote. Barreira adicional.
            raise OperationRefused("packaged_build", REAL_OPERATIONS_BLOCKED_MESSAGE)
        # Reabre a área para conferir marcador e token antes de qualquer uso.
        self._area = IsolatedTestArea.open(area.root, area.token)
        self._store = store
        self._fault = fault or (lambda _checkpoint: None)

    # Autorização -----------------------------------------------------------

    def _authorize(self, item: OperationItem) -> None:
        plan = self._store.plan(item.plan_id)
        if plan is None:
            raise OperationRefused("plan_missing", "Plano inexistente.")
        if plan.environment != ENV_TEST or not plan.test_root:
            raise OperationRefused("real_operations_blocked", REAL_OPERATIONS_BLOCKED_MESSAGE)
        if os.path.normcase(os.path.normpath(plan.test_root)) != os.path.normcase(self._area.root):
            raise OperationRefused(
                "outside_area", "O plano não pertence a esta área de testes isolada."
            )

    def _load(self, item_id: int) -> OperationItem:
        item = self._store.item(item_id)
        if item is None:
            raise OperationRefused("item_missing", "Ação inexistente.")
        self._authorize(item)
        return item

    def _paths(self, item: OperationItem) -> tuple[str, str | None]:
        source = self._area.resolve(item.source_path)
        target = self._area.resolve(item.destination_path) if item.destination_path else None
        return source, target

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
        self._store.add_event(item.plan_id, item.id, "info", "validated", "Ação validada na área de testes.")
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
        self._store.add_event(item.plan_id, item.id, "info", "running", "Execução iniciada (área de testes).")

        temp = None
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
                self._make_parents(target)
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
        except (OperationRefused, UnsafePath) as exc:
            self._cleanup_temp(temp)
            self._store.transition(item.id, (STATE_RUNNING,), STATE_BLOCKED, block_reason=exc.message)
            self._store.finish_execution(execution, STATE_BLOCKED, None, exc.message)
            self._store.add_event(item.plan_id, item.id, "warning", exc.code, exc.message)
            return STATE_BLOCKED
        except Exception as exc:  # noqa: BLE001 - qualquer falha física é registrada
            self._cleanup_temp(temp)
            message = f"Falha durante a execução: {exc}. A origem foi preservada."
            logger.exception("Falha na execução de teste")
            self._store.transition(item.id, (STATE_RUNNING,), STATE_FAILED, block_reason=message)
            self._store.finish_execution(execution, STATE_FAILED, None, message)
            self._store.add_event(item.plan_id, item.id, "error", "failed", message)
            return STATE_FAILED

    # Reversão ----------------------------------------------------------------

    def revert(self, item_id: int) -> str:
        item = self._load(item_id)
        if item.state != STATE_COMPLETED or not item.reversible:
            message = (
                "Cópia não requer reversão: a origem foi preservada."
                if item.action_type == ACTION_COPY
                else "Somente movimentações concluídas e conferidas podem ser revertidas."
            )
            raise OperationRefused("not_reversible", message)
        recovery = self._store.start_recovery(item.id)
        if not self._store.transition(item.id, (STATE_COMPLETED,), STATE_REVERT_PENDING):
            self._store.finish_recovery(recovery, "blocked", "Estado mudou antes da reversão.")
            return self._store.item(item.id).state
        temp = None
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
            self._make_parents(original)
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
        except (OperationRefused, UnsafePath) as exc:
            self._cleanup_temp(temp)
            self._store.transition(item.id, (STATE_REVERT_PENDING,), STATE_COMPLETED)
            self._store.finish_recovery(recovery, "blocked", exc.message)
            self._store.add_event(item.plan_id, item.id, "warning", exc.code, exc.message)
            return STATE_COMPLETED
        except Exception as exc:  # noqa: BLE001
            self._cleanup_temp(temp)
            message = f"Falha na reversão: {exc}. Nenhum arquivo foi removido sem cópia conferida."
            logger.exception("Falha na reversão de teste")
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
        removed = self._remove_own_temps(item, target) + self._remove_own_temps(item, source)
        src_ok = self._matches(source, item.sha256)
        dst_ok = self._matches(target, item.sha256) if target else False
        was_reverting = item.dst_index is not None  # já tinha sido concluída antes

        if item.action_type == ACTION_COPY:
            if src_ok and dst_ok:
                state, note = STATE_COMPLETED, "Cópia encontrada íntegra no destino."
            else:
                state, note = STATE_FAILED, "Cópia não concluída; a origem foi preservada."
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

    def _make_parents(self, target: str) -> None:
        missing = []
        current = os.path.dirname(target)
        while not os.path.lexists(current):
            missing.append(current)
            current = os.path.dirname(current)
        for folder in reversed(missing):
            self._area.resolve(folder)
            os.mkdir(folder)
        self._area.resolve(target)  # confere de novo após criar as pastas

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

    def _remove_own_temps(self, item: OperationItem, path: str | None) -> int:
        if not path:
            return 0
        temp = self._temp_name(item, path)
        if os.path.lexists(temp):
            self._area.resolve(temp)
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
    def _matches(path: str | None, digest: str | None) -> bool:
        if not path or not digest:
            return False
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and not is_link_like(st) and sha256_of(path) == digest
