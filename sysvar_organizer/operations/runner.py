"""Execução em lote, por unidade, das operações reais controladas.

Usado pela interface (em segundo plano) e pelos testes. Toda a autorização é
refeita pelo ``SafeExecutor`` a cada ação; este módulo organiza o lote:

* Validação no disco: reconcilia ações interrompidas, valida cada ação
  SELECIONADA e, se um integrante de uma unidade (RAW/XMP/JPEG) for
  bloqueado, bloqueia a unidade inteira.
* Execução: somente ações selecionadas e validadas, de unidades completas, e
  somente se o conjunto for exatamente o confirmado pelo usuário (``token``
  da confirmação). Se um integrante de uma unidade falhar, as movimentações
  já feitas nessa unidade são revertidas, para não separar os arquivos.
* Reversão: por unidade, somente se TODOS os integrantes passarem na
  conferência prévia (nada é alterado se algum estiver bloqueado).
* Cancelamento: entre unidades e durante a gravação de um arquivo; nunca
  deixa uma unidade separada.
* Cada lote e o resultado de cada arquivo (inclusive ignorados) ficam
  registrados no banco.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.operations.executor import OperationRefused, SafeExecutor
from sysvar_organizer.operations.models import (
    ACTION_COPY,
    ACTION_KEEP,
    STATE_BLOCKED,
    STATE_COMPLETED,
    STATE_INTERRUPTED,
    STATE_PLANNED,
    STATE_REVERTED,
    STATE_VALIDATED,
)
from sysvar_organizer.persistence.operations_store import OperationItem, OperationsStore

logger = logging.getLogger(__name__)

Progress = Callable[[int, int, str], None]
COUNT_KEYS = ("succeeded", "blocked", "failed", "skipped")


@dataclass
class RunOutcome:
    run_id: int | None
    kind: str
    status: str
    message: str
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNT_KEYS, 0))
    results: list[tuple[int, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionSummary:
    """O que será executado, exatamente como apresentado na confirmação final."""

    plan_id: int
    operation: str
    items: tuple[OperationItem, ...]
    units: int
    total_bytes: int
    incomplete_units: int
    token: str

    @property
    def count(self) -> int:
        return len(self.items)


def execution_token(items) -> str:
    """Impressão digital do conjunto confirmado (ações, caminhos e conteúdo)."""
    digest = hashlib.sha256()
    for item in sorted(items, key=lambda i: i.id):
        digest.update(
            f"{item.id}|{item.action_type}|{item.source_path}|{item.destination_path}|"
            f"{item.sha256}|{item.src_index}\n".encode()
        )
    return digest.hexdigest()


class OperationRunner:
    def __init__(
        self,
        store: OperationsStore,
        cancel: threading.Event | None = None,
        progress: Progress | None = None,
        executor: SafeExecutor | None = None,
    ) -> None:
        self._store = store
        self._cancel = cancel or threading.Event()
        self._progress = progress or (lambda _done, _total, _label: None)
        self._executor = executor or SafeExecutor(store, None, cancel=self._cancel)

    # Auxiliares ----------------------------------------------------------

    def _units(self, items) -> OrderedDict[str, list[OperationItem]]:
        units: OrderedDict[str, list[OperationItem]] = OrderedDict()
        for item in items:
            units.setdefault(item.unit_key, []).append(item)
        return units

    def _record(self, outcome: RunOutcome, item_id: int, result: str, message: str, bucket: str) -> None:
        outcome.results.append((item_id, result, message))
        outcome.counts[bucket] += 1
        if outcome.run_id is not None:
            self._store.record_run_item(outcome.run_id, item_id, result, message)

    def _finish(self, outcome: RunOutcome, status: str, message: str) -> RunOutcome:
        outcome.status, outcome.message = status, message
        if outcome.run_id is not None:
            self._store.finish_run(outcome.run_id, status, outcome.counts, message)
        logger.info(
            "Lote de operações encerrado: lote=%s tipo=%s estado=%s concluidas=%d bloqueadas=%d "
            "falhas=%d ignoradas=%d",
            outcome.run_id, outcome.kind, status, outcome.counts["succeeded"],
            outcome.counts["blocked"], outcome.counts["failed"], outcome.counts["skipped"],
        )
        return outcome

    def _guarded(self, kind: str, plan_id: int, work) -> RunOutcome:
        outcome = RunOutcome(None, kind, "running", "")
        try:
            return work(outcome)
        except DatabaseError as exc:
            logger.error("Banco inacessível durante o lote (tipo=%s, categoria=%s)", kind, exc.category)
            outcome.status = "failed"
            outcome.message = (
                "O banco de dados ficou inacessível; o lote foi interrompido com segurança. "
                "Ações em andamento serão reconciliadas na próxima abertura."
            )
            return outcome

    # Validação -------------------------------------------------------------

    def validate_plan(self, plan_id: int) -> RunOutcome:
        return self._guarded("validate", plan_id, lambda outcome: self._validate(plan_id, outcome))

    def _validate(self, plan_id: int, outcome: RunOutcome) -> RunOutcome:
        interrupted = self._store.items(plan_id, state=STATE_INTERRUPTED, limit=100000)
        targets = [
            i for i in self._store.approved_items(plan_id, (STATE_PLANNED, STATE_VALIDATED))
            if i.action_type != ACTION_KEEP
        ]
        outcome.run_id = self._store.start_run(plan_id, "validate", len(targets) + len(interrupted), None)
        total = len(targets) + len(interrupted)
        done = 0
        for item in interrupted:
            try:
                state = self._executor.reconcile(item.id)
                self._record(outcome, item.id, "reconciled", f"Reconciliada: {state}.", "succeeded")
            except OperationRefused as exc:
                self._record(outcome, item.id, "blocked", exc.message, "blocked")
            done += 1
            self._progress(done, total, "Reconciliando ações interrompidas")
        for item in targets:
            if self._cancel.is_set():
                self._record(outcome, item.id, "skipped", "Validação cancelada antes desta ação.", "skipped")
                continue
            self._progress(done, total, item.source_path)
            try:
                state = self._executor.validate(item.id)
            except OperationRefused as exc:
                if exc.code in ("real_operations_blocked", "area_revoked", "area_changed", "wrong_scope"):
                    self._record(outcome, item.id, "blocked", exc.message, "blocked")
                    return self._finish(outcome, "failed", exc.message)
                self._record(outcome, item.id, "blocked", exc.message, "blocked")
                continue
            finally:
                done += 1
            current = self._store.item(item.id)
            if state == STATE_VALIDATED:
                self._record(outcome, item.id, "validated", "Validada no disco.", "succeeded")
            else:
                self._record(outcome, item.id, "blocked", current.block_reason or "Bloqueada.", "blocked")
        self._block_broken_units(plan_id, outcome)
        if self._cancel.is_set():
            return self._finish(outcome, "cancelled", "Validação cancelada; nenhuma operação física foi feita.")
        return self._finish(outcome, "completed", "Validação concluída; nenhuma operação física foi feita.")

    def _block_broken_units(self, plan_id: int, outcome: RunOutcome) -> None:
        """Integrante bloqueado bloqueia a unidade inteira (arquivos seguem juntos)."""
        validated = self._store.approved_items(plan_id, (STATE_VALIDATED,))
        for unit_key, members in self._units(validated).items():
            all_members = self._store.unit_members(plan_id, unit_key)
            broken = [m for m in all_members if m.state == STATE_BLOCKED or not m.approved]
            if not broken:
                continue
            reason = "Arquivo associado bloqueado ou não selecionado: a unidade não pode ser separada."
            for member in members:
                if self._store.transition(member.id, (STATE_VALIDATED,), STATE_BLOCKED, block_reason=reason):
                    self._store.add_event(plan_id, member.id, "warning", "dependency", reason)
                    self._record(outcome, member.id, "blocked", reason, "blocked")

    # Execução --------------------------------------------------------------

    def summary(self, plan_id: int) -> ExecutionSummary:
        """Ações que SERÃO executadas: selecionadas, validadas e de unidades completas."""
        plan = self._store.plan(plan_id)
        validated = [
            i for i in self._store.approved_items(plan_id, (STATE_VALIDATED,))
            if i.action_type != ACTION_KEEP
        ]
        ready: list[OperationItem] = []
        incomplete = 0
        units = 0
        for unit_key, members in self._units(validated).items():
            all_members = [
                m for m in self._store.unit_members(plan_id, unit_key) if m.action_type != ACTION_KEEP
            ]
            pending = [m for m in all_members if m.state != STATE_COMPLETED]
            if all(m.state == STATE_VALIDATED and m.approved for m in pending):
                ready.extend(members)
                units += 1
            else:
                incomplete += 1
        return ExecutionSummary(
            plan_id,
            plan.operation if plan else "",
            tuple(ready),
            units,
            sum(i.size_bytes for i in ready),
            incomplete,
            execution_token(ready),
        )

    def execute_plan(self, plan_id: int, token: str) -> RunOutcome:
        return self._guarded("execute", plan_id, lambda outcome: self._execute(plan_id, token, outcome))

    def _execute(self, plan_id: int, token: str, outcome: RunOutcome) -> RunOutcome:
        summary = self.summary(plan_id)
        if summary.token != token:
            raise OperationRefused(
                "confirmation_mismatch",
                "As ações mudaram depois da confirmação; revise e confirme novamente.",
            )
        if not summary.items:
            raise OperationRefused("nothing_to_execute", "Não há ações selecionadas e validadas.")
        outcome.run_id = self._store.start_run(plan_id, "execute", summary.count, token)
        done = 0
        for unit_key, members in self._units(summary.items).items():
            if self._cancel.is_set():
                for member in members:
                    self._record(outcome, member.id, "skipped", "Execução cancelada antes desta unidade.", "skipped")
                continue
            completed: list[OperationItem] = []
            for position, member in enumerate(members):
                self._progress(done, summary.count, member.source_path)
                try:
                    state = self._executor.execute(member.id)
                except OperationRefused as exc:
                    state, note = STATE_BLOCKED, exc.message
                else:
                    note = self._store.item(member.id).block_reason or ""
                done += 1
                if state == STATE_COMPLETED:
                    self._record(outcome, member.id, "completed", "Concluída e conferida.", "succeeded")
                    completed.append(member)
                    continue
                cancelled = state == STATE_VALIDATED  # o executor só devolve "validada" ao cancelar
                if cancelled:
                    self._record(outcome, member.id, "cancelled", "Cancelada durante a gravação; nada foi alterado.", "skipped")
                else:
                    bucket = "blocked" if state == STATE_BLOCKED else "failed"
                    self._record(outcome, member.id, bucket, note or "Não concluída.", bucket)
                for rest in members[position + 1:]:
                    self._record(outcome, rest.id, "skipped",
                                 "Não executada: outro arquivo da mesma unidade não foi concluído.", "skipped")
                self._rollback_unit(completed, outcome)
                break
        if self._cancel.is_set():
            return self._finish(outcome, "cancelled", "Execução cancelada; unidades não iniciadas foram ignoradas.")
        return self._finish(outcome, "completed", "Execução concluída. Confira o resultado de cada arquivo.")

    def _rollback_unit(self, completed: list[OperationItem], outcome: RunOutcome) -> None:
        """Desfaz as movimentações já feitas numa unidade que não pôde ser concluída."""
        for member in completed:
            if member.action_type == ACTION_COPY:
                message = (
                    "Unidade incompleta no destino: a origem permanece intacta; a cópia feita "
                    "foi mantida (cópias não são removidas automaticamente)."
                )
                self._store.add_event(member.plan_id, member.id, "warning", "unit_incomplete", message)
                self._record(outcome, member.id, "skipped", message, "skipped")
                continue
            try:
                state = self._executor.revert(member.id)
            except OperationRefused as exc:
                state = exc.message
            if state == STATE_REVERTED:
                message = "Movimentação desfeita para não separar a unidade."
                self._record(outcome, member.id, "rolled_back", message, "skipped")
            else:
                message = f"Não foi possível desfazer a movimentação ({state}); revisão manual necessária."
                self._store.add_event(member.plan_id, member.id, "error", "rollback_failed", message)
                self._record(outcome, member.id, "failed", message, "failed")

    # Reversão --------------------------------------------------------------

    def revert_unit(self, plan_id: int, unit_key: str) -> RunOutcome:
        return self._guarded("revert", plan_id, lambda outcome: self._revert(plan_id, unit_key, outcome))

    def _revert(self, plan_id: int, unit_key: str, outcome: RunOutcome) -> RunOutcome:
        members = [
            m for m in self._store.unit_members(plan_id, unit_key)
            if m.state == STATE_COMPLETED and m.reversible
        ]
        if not members:
            raise OperationRefused(
                "not_reversible",
                "Nenhuma movimentação concluída e reversível nesta unidade (cópias não são revertidas).",
            )
        outcome.run_id = self._store.start_run(plan_id, "revert", len(members), None)
        problems = [(m, self._executor.revert_preflight(m.id)) for m in members]
        if any(reason for _m, reason in problems):
            for member, reason in problems:
                message = reason or "Não revertida: outro arquivo da unidade está bloqueado."
                self._store.add_event(plan_id, member.id, "warning", "revert_blocked", message)
                self._record(outcome, member.id, "blocked", message, "blocked")
            return self._finish(outcome, "completed", "Reversão bloqueada; nenhum arquivo foi alterado.")
        for done, member in enumerate(members):
            self._progress(done, len(members), member.destination_path or member.source_path)
            state = self._executor.revert(member.id)
            if state == STATE_REVERTED:
                self._record(outcome, member.id, "reverted", "Revertida e conferida.", "succeeded")
            else:
                recoveries = self._store.recoveries(member.id)
                reason = (recoveries[-1][3] if recoveries else None) or "Reversão não concluída."
                bucket = "failed" if state != STATE_COMPLETED else "blocked"
                self._record(outcome, member.id, bucket, reason, bucket)
        return self._finish(outcome, "completed", "Reversão concluída. Confira o resultado de cada arquivo.")
