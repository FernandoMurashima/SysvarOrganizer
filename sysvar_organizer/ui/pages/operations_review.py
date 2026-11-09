"""Revisão de operações (seção da página Organização).

Permite criar um plano a partir da prévia concluída, selecionar ou desmarcar
unidades, ver conflitos, dependências, alertas e detalhes, cancelar o
planejamento e — SOMENTE em planos de uma área de homologação autorizada —
validar no disco, executar (copiar/mover) e reverter movimentações.

Três modos, sempre visíveis:

* SIMULAÇÃO: a prévia (nada é alterado);
* TESTE REAL CONTROLADO: plano criado dentro de áreas autorizadas pelo
  usuário; executa após seleção, validação e confirmação final;
* OPERAÇÃO REAL NÃO AUTORIZADA: plano sem área autorizada (inclusive todos os
  anteriores ao Bloco 10); pode ser revisado, nunca executado.

As tarefas físicas rodam fora da thread da interface, com progresso e
cancelamento seguro. Envio à Lixeira e exclusão não existem nesta fase.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QTableWidgetItem,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.operations.authorization import AuthorizedArea, area_for, check_area_root
from sysvar_organizer.operations.models import (
    ACTION_COPY,
    ACTION_KEEP,
    ACTION_LABELS,
    ACTION_MOVE,
    CONTROLLED_OPERATIONS_MESSAGE,
    ENV_REAL,
    MODE_CONTROLLED,
    MODE_SIMULATION,
    MODE_UNAUTHORIZED,
    REAL_OPERATIONS_BLOCKED_MESSAGE,
    STATE_COMPLETED,
    STATE_INTERRUPTED,
    STATE_LABELS,
    STATE_PLANNED,
    STATE_VALIDATED,
    display_state,
    plan_mode,
)
from sysvar_organizer.operations.planning import PlanningError, build_plan
from sysvar_organizer.operations.runner import OperationRunner, RunOutcome
from sysvar_organizer.operations.safety import UnsafePath
from sysvar_organizer.persistence.operations_store import AreaRecord, OperationItem, PlanRecord
from sysvar_organizer.persistence.organization_repository import SimulationRecord
from sysvar_organizer.ui.background import BackgroundTask
from sysvar_organizer.ui.formatting import format_bytes, format_count, format_timestamp
from sysvar_organizer.ui.widgets import Pager, badge, card, label, result_table

logger = logging.getLogger(__name__)

PAGE_SIZE = 200
RESULT_TEXT = {
    "validated": "validada",
    "completed": "concluída",
    "reverted": "revertida",
    "blocked": "bloqueada",
    "failed": "falhou",
    "skipped": "ignorada",
    "cancelled": "cancelada",
    "reconciled": "reconciliada",
    "rolled_back": "desfeita para manter a unidade",
}


def _item(text: str, *, right: bool = False, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def is_controlled(plan: PlanRecord | None) -> bool:
    return plan is not None and plan.environment == ENV_REAL and plan.area_id is not None


def reversibility(item: OperationItem, plan: PlanRecord | None = None) -> str:
    if item.action_type == ACTION_KEEP:
        return "Não se aplica (nenhuma alteração no arquivo)."
    if item.action_type == ACTION_COPY:
        return "Não se aplica: a cópia preserva a origem (cópias não são removidas como reversão)."
    if item.reversible and item.state == STATE_COMPLETED:
        return "Sim: movimentação concluída e conferida (identidade e SHA-256 registrados)."
    if plan is not None and not is_controlled(plan):
        return "Não: plano sem área autorizada (operação real não autorizada)."
    return "Somente após a movimentação ser concluída e conferida."


def plan_title(plan: PlanRecord) -> str:
    status = "cancelado" if plan.status == "cancelled" else "aberto"
    return (
        f"Plano nº {plan.id}  ·  {format_timestamp(plan.created_at)}  ·  "
        f"{ACTION_LABELS.get(plan.operation, plan.operation)}  ·  "
        f"{plan_mode(plan.environment, plan.area_id)}  ·  {status}"
    )


def area_record_to_area(record: AreaRecord) -> AuthorizedArea:
    return AuthorizedArea(record.id, record.root, record.volume_id, record.dir_index, record.status)


def describe_outcome(outcome: RunOutcome) -> str:
    counts = outcome.counts
    lines = [
        outcome.message,
        "",
        f"Concluídas: {counts['succeeded']}  ·  Bloqueadas: {counts['blocked']}  ·  "
        f"Falhas: {counts['failed']}  ·  Ignoradas: {counts['skipped']}",
    ]
    problems = [r for r in outcome.results if r[1] not in ("validated", "completed", "reverted", "reconciled")]
    if problems:
        lines.append("")
        lines.append("Arquivos que precisam de atenção:")
        for item_id, result, message in problems[:8]:
            lines.append(f"• Ação nº {item_id}: {RESULT_TEXT.get(result, result)} — {message}")
        if len(problems) > 8:
            lines.append(f"… e mais {len(problems) - 8}. Consulte o Histórico.")
    return "\n".join(lines)


class OperationsReviewPanel(QWidget):
    plans_changed = Signal()
    running_changed = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._simulation: SimulationRecord | None = None
        self._plans: list[PlanRecord] = []
        self._rows: list[OperationItem] = []
        self._areas: list[AreaRecord] = []
        self._locked = False
        self._busy_check: Callable[[], bool] | None = None
        self._task = BackgroundTask(self)

        frame, box = card()
        outer = QGridLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame, 0, 0)

        heading = QHBoxLayout()
        heading.addWidget(label("Revisão de operações", "SectionTitle"))
        heading.addStretch(1)
        self._mode_badges = {
            MODE_SIMULATION: badge(MODE_SIMULATION, "info"),
            MODE_CONTROLLED: badge(MODE_CONTROLLED, "unavailable"),
            MODE_UNAUTHORIZED: badge(MODE_UNAUTHORIZED, "error"),
        }
        for widget in self._mode_badges.values():
            heading.addWidget(widget)
        box.addLayout(heading)
        self._mode_text = label("", "BodyText", wrap=True)
        box.addWidget(self._mode_text)
        box.addWidget(
            label(
                "Nenhuma ação é considerada aprovada por aparecer na prévia: cada unidade precisa "
                "ser selecionada. Não existe exclusão definitiva e o envio à Lixeira está "
                "desabilitado. Duplicados e fotografias semelhantes aparecem apenas como alertas.",
                "MetricHint",
                wrap=True,
            )
        )

        # Áreas autorizadas -----------------------------------------------
        box.addWidget(label("Áreas autorizadas para homologação", "FieldLabel"))
        areas = QHBoxLayout()
        self._area_combo = QComboBox()
        self._area_combo.setObjectName("AnalysisCombo")
        self._authorize_button = QPushButton("Autorizar pasta de homologação…")
        self._revoke_button = QPushButton("Revogar autorização")
        self._authorize_button.clicked.connect(self._authorize_area)
        self._revoke_button.clicked.connect(self._revoke_area)
        areas.addWidget(self._area_combo, 1)
        areas.addWidget(self._authorize_button)
        areas.addWidget(self._revoke_button)
        box.addLayout(areas)
        box.addWidget(
            label(
                "Operações reais só acontecem dentro destas pastas. A prévia (origem e destino) "
                "precisa estar inteira dentro de áreas autorizadas para o plano ser executável.",
                "MetricHint",
                wrap=True,
            )
        )

        create = QHBoxLayout()
        self._operation_combo = QComboBox()
        self._operation_combo.setObjectName("AnalysisCombo")
        self._operation_combo.addItem(ACTION_LABELS[ACTION_MOVE], ACTION_MOVE)
        self._operation_combo.addItem(ACTION_LABELS[ACTION_COPY], ACTION_COPY)
        self._create_button = QPushButton("Criar plano a partir da prévia exibida")
        self._create_button.clicked.connect(self._create_plan)
        create.addWidget(self._operation_combo)
        create.addWidget(self._create_button)
        create.addStretch(1)
        box.addLayout(create)

        box.addWidget(label("Plano selecionado", "FieldLabel"))
        self._plan_combo = QComboBox()
        self._plan_combo.setObjectName("AnalysisCombo")
        self._plan_combo.currentIndexChanged.connect(lambda *_: self._load_items(0))
        box.addWidget(self._plan_combo)
        self._plan_summary = label("", "MetricHint", wrap=True)
        box.addWidget(self._plan_summary)

        filters = QHBoxLayout()
        self._state_filter = QComboBox()
        self._state_filter.setObjectName("AnalysisCombo")
        self._state_filter.addItem("Todos os estados", None)
        for key, text in STATE_LABELS.items():
            self._state_filter.addItem(text, key)
        self._action_filter = QComboBox()
        self._action_filter.setObjectName("AnalysisCombo")
        self._action_filter.addItem("Todos os tipos de ação", None)
        for key, text in ACTION_LABELS.items():
            self._action_filter.addItem(text, key)
        self._state_filter.currentIndexChanged.connect(lambda *_: self._load_items(0))
        self._action_filter.currentIndexChanged.connect(lambda *_: self._load_items(0))
        filters.addWidget(self._state_filter)
        filters.addWidget(self._action_filter)
        filters.addStretch(1)
        box.addLayout(filters)

        self._table = result_table(
            ["Selecionada", "Ação", "Origem", "Destino", "Estado", "Alertas"], (2, 3)
        )
        self._table.setMinimumHeight(240)
        self._table.itemSelectionChanged.connect(self._on_selected)
        box.addWidget(self._table)
        self._pager = Pager()
        self._pager.page_changed.connect(self._load_items)
        box.addWidget(self._pager)

        actions = QHBoxLayout()
        self._approve_button = QPushButton("Selecionar ação (unidade)")
        self._reject_button = QPushButton("Desmarcar ação (unidade)")
        self._cancel_plan_button = QPushButton("Cancelar planejamento")
        self._approve_button.clicked.connect(lambda: self._set_approval(True))
        self._reject_button.clicked.connect(lambda: self._set_approval(False))
        self._cancel_plan_button.clicked.connect(self._cancel_plan)
        for button in (self._approve_button, self._reject_button, self._cancel_plan_button):
            actions.addWidget(button)
        actions.addStretch(1)
        box.addLayout(actions)

        physical = QHBoxLayout()
        self._validate_button = QPushButton("Validar no disco")
        self._execute_button = QPushButton("Executar operações")
        self._revert_button = QPushButton("Reverter")
        self._cancel_run_button = QPushButton("Cancelar execução")
        self._validate_button.clicked.connect(self._validate)
        self._execute_button.clicked.connect(self._execute)
        self._revert_button.clicked.connect(self._revert)
        self._cancel_run_button.clicked.connect(self._task.cancel)
        # Botões que alteram arquivos: habilitados só em plano controlado.
        self._physical_buttons = [self._validate_button, self._execute_button, self._revert_button]
        for button in (*self._physical_buttons, self._cancel_run_button):
            physical.addWidget(button)
        physical.addStretch(1)
        box.addLayout(physical)
        self._run_status = label("", "MetricHint", wrap=True)
        box.addWidget(self._run_status)

        box.addSpacing(6)
        box.addWidget(label("Detalhes da ação selecionada", "SectionTitle"))
        details = QGridLayout()
        details.setHorizontalSpacing(24)
        details.setVerticalSpacing(6)
        details.setColumnStretch(1, 1)
        self._details = {}
        for index, (key, title) in enumerate(
            (
                ("source", "Origem"),
                ("destination", "Destino"),
                ("action", "Tipo de ação"),
                ("justification", "Justificativa"),
                ("identity", "Tamanho e identificação"),
                ("state", "Estado"),
                ("block", "Bloqueio / validação"),
                ("alerts", "Alertas"),
                ("dependencies", "Dependências"),
                ("reversible", "Reversível"),
                ("history", "Execuções e recuperações"),
            )
        ):
            details.addWidget(label(title, "FieldLabel"), index, 0, Qt.AlignmentFlag.AlignTop)
            value = label("—", "FieldValue", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.addWidget(value, index, 1)
            self._details[key] = value
        box.addLayout(details)

        self.reload_areas()
        self.reload_plans()

    # Tarefas ---------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        self._busy_check = check

    def is_running(self) -> bool:
        return self._task.is_running()

    def request_shutdown(self, timeout_ms: int = 15000) -> bool:
        """Cancela com segurança (entre arquivos/unidades) e aguarda o término."""
        if not self.is_running():
            return True
        if not self._task.wait(timeout_ms):
            logger.warning("A operação física não terminou no tempo de espera do encerramento")
            return False
        return True

    # Dados -----------------------------------------------------------------

    def set_simulation(self, record: SimulationRecord | None) -> None:
        self._simulation = record
        self._update_controls()

    def set_locked(self, locked: bool) -> None:
        """Bloqueia a criação de planos enquanto uma simulação está em andamento."""
        self._locked = locked
        self._update_controls()

    def reload_areas(self) -> None:
        store = self._context.operations
        current = self._area_combo.currentData()
        self._areas = []
        if store is not None:
            try:
                self._areas = store.areas()
            except DatabaseError:
                logger.exception("Falha ao listar as áreas autorizadas")
        self._area_combo.blockSignals(True)
        self._area_combo.clear()
        if not self._areas:
            self._area_combo.addItem("Nenhuma área autorizada (operações reais indisponíveis)", None)
        for area in self._areas:
            self._area_combo.addItem(
                f"{area.root}  ·  autorizada em {format_timestamp(area.created_at)}", area.id
            )
        if current is not None:
            found = self._area_combo.findData(current)
            if found >= 0:
                self._area_combo.setCurrentIndex(found)
        self._area_combo.blockSignals(False)

    def reload_plans(self, select_id: int | None = None) -> None:
        store = self._context.operations
        current = select_id if select_id is not None else self._plan_combo.currentData()
        self._plans = []
        if store is not None:
            try:
                self._plans = store.plans()
            except DatabaseError:
                logger.exception("Falha ao listar os planos de operação")
        self._plan_combo.blockSignals(True)
        self._plan_combo.clear()
        for plan in self._plans:
            self._plan_combo.addItem(plan_title(plan), plan.id)
        if current is not None:
            index = self._plan_combo.findData(current)
            if index >= 0:
                self._plan_combo.setCurrentIndex(index)
        self._plan_combo.blockSignals(False)
        self._load_items(0)

    def _plan(self) -> PlanRecord | None:
        plan_id = self._plan_combo.currentData()
        return next((p for p in self._plans if p.id == plan_id), None)

    def _area_root(self, area_id: int | None) -> str:
        store = self._context.operations
        if area_id is None or store is None:
            return "—"
        try:
            record = store.area(area_id)
        except DatabaseError:
            return "—"
        if record is None:
            return "—"
        return record.root + (" (autorização revogada)" if record.status != "active" else "")

    # Áreas -----------------------------------------------------------------

    def _authorize_area(self) -> None:
        store = self._context.operations
        if store is None or self.is_running():
            return
        folder = QFileDialog.getExistingDirectory(self, "Escolha a pasta de homologação")
        if not folder:
            return
        try:
            root, volume, index = check_area_root(
                os.path.normpath(folder), [area.root for area in self._areas]
            )
        except UnsafePath as exc:
            QMessageBox.warning(self, APP_NAME, f"Esta pasta não pode ser autorizada.\n\n{exc.message}")
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Autorizar esta pasta como área de homologação?\n\n"
            f"{root}\n\n"
            "Arquivos DENTRO desta pasta poderão ser copiados ou movidos pelo Organizer, "
            "somente após você selecionar cada unidade, validar no disco e confirmar a "
            "execução. Nada fora dela poderá ser alterado. Use uma pasta com cópias de teste, "
            "não a sua coleção original.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            area_id = store.create_area(root, volume, index)
        except DatabaseError:
            logger.exception("Falha ao registrar a área autorizada")
            QMessageBox.warning(self, APP_NAME, "Não foi possível registrar a autorização.")
            return
        logger.info("Área de homologação autorizada (área=%s)", area_id)
        self.reload_areas()
        self._area_combo.setCurrentIndex(max(0, self._area_combo.findData(area_id)))
        self._update_controls()

    def _revoke_area(self) -> None:
        store, area_id = self._context.operations, self._area_combo.currentData()
        if store is None or area_id is None or self.is_running():
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Revogar a autorização desta área? Planos vinculados a ela deixarão de poder ser "
            "validados, executados ou revertidos (os registros são preservados).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            store.revoke_area(area_id)
        except DatabaseError:
            logger.exception("Falha ao revogar a área")
        logger.info("Autorização de área revogada (área=%s)", area_id)
        self.reload_areas()
        self._load_items(self._pager.page)

    # Planos ----------------------------------------------------------------

    def _create_plan(self) -> None:
        store, record = self._context.operations, self._simulation
        if store is None or record is None:
            return
        areas = [area_record_to_area(a) for a in self._areas]
        source_area = area_for(record.root, areas)
        destination_area = area_for(record.destination, areas)
        controlled = source_area is not None and destination_area is not None
        if not controlled:
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "A origem ou o destino desta prévia não está dentro de uma área autorizada.\n\n"
                "O plano será criado como OPERAÇÃO REAL NÃO AUTORIZADA: poderá ser revisado, "
                "mas não executado. Deseja criá-lo assim mesmo?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan_id = build_plan(
                store,
                record.id,
                self._operation_combo.currentData(),
                ENV_REAL,
                source_area=source_area if controlled else None,
                destination_area=destination_area if controlled else None,
            )
        except (PlanningError, DatabaseError, UnsafePath) as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.information(self, APP_NAME, str(exc))
            return
        QApplication.restoreOverrideCursor()
        self.reload_plans(plan_id)
        self.plans_changed.emit()

    def _load_items(self, page: int) -> None:
        self._table.setRowCount(0)
        self._rows = []
        self._clear_details()
        plan, store = self._plan(), self._context.operations
        self._show_mode(plan)
        if plan is None or store is None:
            self._plan_summary.setText("Nenhum plano criado." if not self._plans else "")
            self._pager.set_total(0, PAGE_SIZE)
            self._update_controls()
            return
        state = self._state_filter.currentData()
        action = self._action_filter.currentData()
        try:
            counts = store.state_counts(plan.id)
            approved = store.approved_count(plan.id)
            total = store.count_items(plan.id, state, action)
            self._pager.set_total(total, PAGE_SIZE, page)
            self._rows = store.items(plan.id, state, action, self._pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar o plano")
            return
        self._counts, self._approved = counts, approved
        parts = [f"{STATE_LABELS.get(k, k)}: {format_count(v)}" for k, v in sorted(counts.items())]
        areas = ""
        if is_controlled(plan):
            source = self._area_root(plan.area_id)
            target = self._area_root(plan.destination_area_id or plan.area_id)
            areas = f"\nÁrea autorizada: {source}" + (f"  ·  destino: {target}" if target != source else "")
        self._plan_summary.setText(
            f"{format_count(plan.items_count)} ações  ·  {format_count(approved)} selecionadas  ·  "
            + "  ·  ".join(parts)
            + f"\nOperação: {ACTION_LABELS.get(plan.operation, plan.operation)}  ·  "
            f"Destino do plano: {plan.destination}"
            + areas
        )
        self._table.setRowCount(len(self._rows))
        for index, item in enumerate(self._rows):
            self._table.setItem(index, 0, _item("Sim" if item.approved else "Não"))
            self._table.setItem(index, 1, _item(ACTION_LABELS.get(item.action_type, item.action_type)))
            self._table.setItem(index, 2, _item(item.source_path, tooltip=item.source_path))
            target = item.destination_path or "— (permanece no local)"
            self._table.setItem(index, 3, _item(target, tooltip=target))
            state_text = display_state(item.state, plan.environment, plan.area_id)
            self._table.setItem(index, 4, _item(state_text))
            warning = item.block_reason or item.alerts or ("Unidade associada" if item.dependencies else "")
            self._table.setItem(index, 5, _item(warning, tooltip=warning))
        self._update_controls()

    def _show_mode(self, plan: PlanRecord | None) -> None:
        if plan is None:
            mode = MODE_SIMULATION
            text = (
                "SIMULAÇÃO: a prévia não altera arquivos. Crie um plano para revisar as ações; "
                "ele só será executável se a prévia estiver dentro de uma área autorizada."
            )
        else:
            mode = plan_mode(plan.environment, plan.area_id)
            text = CONTROLLED_OPERATIONS_MESSAGE if mode == MODE_CONTROLLED else REAL_OPERATIONS_BLOCKED_MESSAGE
        for key, widget in self._mode_badges.items():
            widget.setVisible(key == mode)
        self._mode_text.setText(text)

    def _selected_item(self) -> OperationItem | None:
        index = self._table.currentRow()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def _on_selected(self) -> None:
        item, plan, store = self._selected_item(), self._plan(), self._context.operations
        if item is None or plan is None or store is None:
            self._clear_details()
            self._update_controls()
            return
        history = []
        try:
            for kind, prior, started, _finished, result, method, message in store.executions(item.id):
                history.append(
                    f"{format_timestamp(started)} {kind} ({prior} → {result or 'em andamento'})"
                    f"{': ' + method if method else ''}{' — ' + message if message else ''}"
                )
            for requested, _finished, result, message in store.recoveries(item.id):
                history.append(f"{format_timestamp(requested)} reversão: {result or 'pendente'} — {message or ''}")
        except DatabaseError:
            logger.exception("Falha ao consultar execuções")
        identity = f"{format_bytes(item.size_bytes)} ({format_count(item.size_bytes)} bytes)"
        if item.sha256:
            identity += f"  ·  SHA-256 {item.sha256}"
        if item.src_index:
            identity += f"  ·  volume {item.src_volume}, arquivo {item.src_index}"
        values = {
            "source": item.source_path,
            "destination": item.destination_path or "Permanece no local.",
            "action": ACTION_LABELS.get(item.action_type, item.action_type)
            + ("  (selecionada)" if item.approved else "  (não selecionada)"),
            "justification": item.justification,
            "identity": identity,
            "state": display_state(item.state, plan.environment, plan.area_id),
            "block": item.block_reason or "Nenhum.",
            "alerts": item.alerts or "Nenhum.",
            "dependencies": item.dependencies or "Nenhuma (ação individual).",
            "reversible": reversibility(item, plan),
            "history": "\n".join(history) or "Nenhuma execução registrada.",
        }
        for key, text in values.items():
            self._details[key].setText(text)
        self._update_controls()

    def _set_approval(self, approved: bool) -> None:
        item, plan, store = self._selected_item(), self._plan(), self._context.operations
        if item is None or plan is None or store is None or self.is_running():
            return
        try:
            changed = store.set_unit_approval(plan.id, item.unit_key, approved)
        except DatabaseError:
            logger.exception("Falha ao registrar a seleção")
            return
        if approved and changed == 0:
            QMessageBox.information(
                self,
                APP_NAME,
                "Esta ação (ou um arquivo associado a ela) está bloqueada ou o plano foi "
                "cancelado; ela não pode ser selecionada.",
            )
        self._load_items(self._pager.page)

    def _cancel_plan(self) -> None:
        plan, store = self._plan(), self._context.operations
        if plan is None or store is None or plan.status != "open" or self.is_running():
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Cancelar este planejamento? As seleções serão desfeitas e nenhuma ação pendente será "
            "executada. Ações já concluídas continuam registradas e podem ser revertidas.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            try:
                store.cancel_plan(plan.id)
            except DatabaseError:
                logger.exception("Falha ao cancelar o planejamento")
            self.reload_plans(plan.id)
            self.plans_changed.emit()

    # Operações físicas -------------------------------------------------------

    def _can_start(self) -> bool:
        if self.is_running() or self._context.operations is None:
            return False
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self, APP_NAME, "Há outra tarefa em andamento. Aguarde a conclusão para continuar."
            )
            return False
        return True

    def _start(self, title: str, work: Callable[[OperationRunner], RunOutcome]) -> None:
        store = self._context.operations

        def task(cancel, progress):
            runner = OperationRunner(store, cancel, lambda done, total, _label: progress(done, total))
            return work(runner)

        self._run_status.setText(f"{title}…")
        self._task.start(task, self._on_finished, self._on_progress, self._on_done)
        self._update_controls()
        self.running_changed.emit()

    def _on_progress(self, done: int, total: int) -> None:
        self._run_status.setText(f"Processando {format_count(done)} de {format_count(total)}…")

    def _on_finished(self, outcome, error) -> None:
        if error is not None:
            message = getattr(error, "message", None) or (
                str(error) if isinstance(error, DatabaseError) else
                "A operação não pôde ser concluída. Detalhes no log do aplicativo."
            )
            if not getattr(error, "message", None) and not isinstance(error, DatabaseError):
                logger.error("Falha inesperada na operação física", exc_info=error)
            self._run_status.setText(message)
            QMessageBox.warning(self, APP_NAME, message)
            return
        self._run_status.setText(
            f"{outcome.message}  Concluídas: {outcome.counts['succeeded']} · bloqueadas: "
            f"{outcome.counts['blocked']} · falhas: {outcome.counts['failed']} · ignoradas: "
            f"{outcome.counts['skipped']}."
        )
        show = QMessageBox.information if not (outcome.counts["failed"] or outcome.status == "failed") else QMessageBox.warning
        show(self, APP_NAME, describe_outcome(outcome))

    def _on_done(self) -> None:
        self.reload_plans()
        self.plans_changed.emit()
        self.running_changed.emit()

    def _validate(self) -> None:
        plan = self._plan()
        if not is_controlled(plan) or plan.status != "open" or not self._can_start():
            return
        plan_id = plan.id
        self._start("Validando no disco", lambda runner: runner.validate_plan(plan_id))

    def _execute(self) -> None:
        plan, store = self._plan(), self._context.operations
        if not is_controlled(plan) or plan.status != "open" or not self._can_start():
            return
        try:
            summary = OperationRunner(store).summary(plan.id)
        except DatabaseError:
            logger.exception("Falha ao preparar a execução")
            return
        if not summary.count:
            QMessageBox.information(
                self,
                APP_NAME,
                "Não há ações selecionadas e validadas para executar. Selecione as unidades e "
                "use \"Validar no disco\" antes.",
            )
            return
        origin = self._simulation.root if self._simulation is not None else self._area_root(plan.area_id)
        incomplete = (
            f"\nUnidades incompletas que serão IGNORADAS: {summary.incomplete_units}"
            if summary.incomplete_units
            else ""
        )
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Confirmar a execução REAL?\n\n"
            f"Operação: {ACTION_LABELS.get(plan.operation, plan.operation)}\n"
            f"Arquivos: {format_count(summary.count)} em {format_count(summary.units)} unidade(s) "
            f"({format_bytes(summary.total_bytes)})\n"
            f"Origem: {origin}\n"
            f"Destino: {plan.destination}\n"
            f"Área autorizada: {self._area_root(plan.area_id)}"
            f"{incomplete}\n\n"
            "Cada arquivo é conferido por SHA-256, nada é sobrescrito e movimentações podem ser "
            "revertidas. A execução pode ser cancelada entre arquivos.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        plan_id, token = plan.id, summary.token
        self._start("Executando", lambda runner: runner.execute_plan(plan_id, token))

    def _revert(self) -> None:
        plan, item = self._plan(), self._selected_item()
        if not is_controlled(plan) or item is None or not self._can_start():
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Reverter a movimentação desta unidade?\n\n"
            f"{os.path.basename(item.source_path)}"
            + ("  (e arquivos associados)" if item.dependencies else "")
            + "\n\nOs arquivos voltam ao local original somente se o original estiver livre e o "
            "conteúdo conferir. Nada é sobrescrito.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        plan_id, unit_key = plan.id, item.unit_key
        self._start("Revertendo", lambda runner: runner.revert_unit(plan_id, unit_key))

    # Estado dos controles ----------------------------------------------------

    def _clear_details(self) -> None:
        for value in self._details.values():
            value.setText("—")

    def _update_controls(self) -> None:
        plan = self._plan()
        item = self._selected_item()
        running = self.is_running()
        open_plan = plan is not None and plan.status == "open"
        controlled = is_controlled(plan)
        counts = getattr(self, "_counts", {}) if plan is not None else {}
        approved = getattr(self, "_approved", 0) if plan is not None else 0
        self._create_button.setEnabled(
            self._simulation is not None
            and self._context.operations is not None
            and not self._locked
            and not running
        )
        self._authorize_button.setEnabled(self._context.operations is not None and not running)
        self._revoke_button.setEnabled(self._area_combo.currentData() is not None and not running)
        self._approve_button.setEnabled(open_plan and item is not None and not running)
        self._reject_button.setEnabled(open_plan and item is not None and not running)
        self._cancel_plan_button.setEnabled(open_plan and not running)
        pending = approved > 0 and any(
            counts.get(s, 0) for s in (STATE_PLANNED, STATE_VALIDATED, STATE_INTERRUPTED)
        )
        # Operações reais: somente em plano CONTROLADO (área autorizada).
        self._validate_button.setEnabled(controlled and open_plan and pending and not running)
        self._execute_button.setEnabled(
            controlled and open_plan and counts.get(STATE_VALIDATED, 0) > 0 and not running
        )
        self._revert_button.setEnabled(
            controlled
            and item is not None
            and item.state == STATE_COMPLETED
            and bool(item.reversible)
            and not running
        )
        self._cancel_run_button.setEnabled(running)
        tooltip = "" if controlled else REAL_OPERATIONS_BLOCKED_MESSAGE
        for button in self._physical_buttons:
            button.setToolTip(tooltip)
