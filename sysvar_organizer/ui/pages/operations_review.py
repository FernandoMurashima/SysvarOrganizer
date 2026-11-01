"""Revisão de operações (seção da página Organização).

Permite criar um plano a partir da prévia concluída, consultar as ações,
selecionar ou desmarcar unidades, ver conflitos, dependências, alertas e
detalhes, e cancelar o planejamento. Operações reais estão BLOQUEADAS nesta
etapa: os botões que alterariam arquivos permanecem desabilitados e esta
interface não tem acesso ao executor físico.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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
from sysvar_organizer.operations.models import (
    ACTION_COPY,
    ACTION_KEEP,
    ACTION_LABELS,
    ACTION_MOVE,
    ENV_REAL,
    REAL_OPERATIONS_BLOCKED_MESSAGE,
    STATE_COMPLETED,
    STATE_LABELS,
    display_state,
)
from sysvar_organizer.operations.planning import PlanningError, build_plan
from sysvar_organizer.persistence.operations_store import OperationItem, PlanRecord
from sysvar_organizer.persistence.organization_repository import SimulationRecord
from sysvar_organizer.ui.formatting import format_bytes, format_count, format_timestamp
from sysvar_organizer.ui.widgets import Pager, badge, card, label, result_table

logger = logging.getLogger(__name__)

PAGE_SIZE = 200


def _item(text: str, *, right: bool = False, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def reversibility(item: OperationItem) -> str:
    if item.action_type == ACTION_KEEP:
        return "Não se aplica (nenhuma alteração no arquivo)."
    if item.action_type == ACTION_COPY:
        return "Não se aplica: a cópia preserva a origem."
    if item.reversible and item.state == STATE_COMPLETED:
        return "Sim: movimentação concluída e conferida (identidade e SHA-256 registrados)."
    return "Somente após conclusão conferida; nesta etapa, apenas na área de testes isolada."


def plan_title(plan: PlanRecord) -> str:
    environment = "REAL — execução bloqueada" if plan.environment == ENV_REAL else "TESTE"
    status = "cancelado" if plan.status == "cancelled" else "aberto"
    return (
        f"Plano nº {plan.id}  ·  {format_timestamp(plan.created_at)}  ·  "
        f"{ACTION_LABELS.get(plan.operation, plan.operation)}  ·  {environment}  ·  {status}"
    )


class OperationsReviewPanel(QWidget):
    plans_changed = Signal()

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._simulation: SimulationRecord | None = None
        self._plans: list[PlanRecord] = []
        self._rows: list[OperationItem] = []
        self._locked = False

        frame, box = card()
        outer = QGridLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame, 0, 0)

        heading = QHBoxLayout()
        heading.addWidget(label("Revisão de operações", "SectionTitle"))
        heading.addStretch(1)
        heading.addWidget(badge("Operações reais bloqueadas", "error"))
        box.addLayout(heading)
        box.addWidget(label(REAL_OPERATIONS_BLOCKED_MESSAGE, "BodyText", wrap=True))
        box.addWidget(
            label(
                "Nenhuma ação é considerada aprovada por aparecer na prévia: cada unidade precisa "
                "ser selecionada. Não existe exclusão definitiva. Resultados de duplicados e de "
                "fotografias semelhantes aparecem apenas como alertas.",
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

        blocked = QHBoxLayout()
        self._physical_buttons = []
        for text in ("Validar no disco", "Executar operações", "Reverter"):
            button = QPushButton(text)
            button.setEnabled(False)
            button.setToolTip(REAL_OPERATIONS_BLOCKED_MESSAGE)
            self._physical_buttons.append(button)
            blocked.addWidget(button)
        blocked.addWidget(
            label("Desabilitados: operações reais bloqueadas nesta etapa.", "MetricHint")
        )
        blocked.addStretch(1)
        box.addLayout(blocked)

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

        self.reload_plans()

    # Dados -----------------------------------------------------------------

    def set_simulation(self, record: SimulationRecord | None) -> None:
        self._simulation = record
        self._update_controls()

    def set_locked(self, locked: bool) -> None:
        """Bloqueia a criação de planos enquanto uma simulação está em andamento."""
        self._locked = locked
        self._update_controls()

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

    def _create_plan(self) -> None:
        store, record = self._context.operations, self._simulation
        if store is None or record is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan_id = build_plan(store, record.id, self._operation_combo.currentData(), ENV_REAL)
        except (PlanningError, DatabaseError) as exc:
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
        parts = [f"{STATE_LABELS.get(k, k)}: {format_count(v)}" for k, v in sorted(counts.items())]
        self._plan_summary.setText(
            f"{format_count(plan.items_count)} ações  ·  {format_count(approved)} selecionadas  ·  "
            + "  ·  ".join(parts)
            + f"\nDestino do plano: {plan.destination}"
        )
        self._table.setRowCount(len(self._rows))
        for index, item in enumerate(self._rows):
            self._table.setItem(index, 0, _item("Sim" if item.approved else "Não"))
            self._table.setItem(index, 1, _item(ACTION_LABELS.get(item.action_type, item.action_type)))
            self._table.setItem(index, 2, _item(item.source_path, tooltip=item.source_path))
            target = item.destination_path or "— (permanece no local)"
            self._table.setItem(index, 3, _item(target, tooltip=target))
            self._table.setItem(index, 4, _item(display_state(item.state, plan.environment)))
            warning = item.block_reason or item.alerts or ("Unidade associada" if item.dependencies else "")
            self._table.setItem(index, 5, _item(warning, tooltip=warning))
        self._update_controls()

    def _on_selected(self) -> None:
        index = self._table.currentRow()
        plan, store = self._plan(), self._context.operations
        if not 0 <= index < len(self._rows) or plan is None or store is None:
            self._clear_details()
            return
        item = self._rows[index]
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
            "state": display_state(item.state, plan.environment),
            "block": item.block_reason or "Nenhum.",
            "alerts": item.alerts or "Nenhum.",
            "dependencies": item.dependencies or "Nenhuma (ação individual).",
            "reversible": reversibility(item),
            "history": "\n".join(history) or "Nenhuma execução registrada.",
        }
        for key, text in values.items():
            self._details[key].setText(text)
        self._update_controls()

    def _set_approval(self, approved: bool) -> None:
        index = self._table.currentRow()
        plan, store = self._plan(), self._context.operations
        if not 0 <= index < len(self._rows) or plan is None or store is None:
            return
        item = self._rows[index]
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
        if plan is None or store is None or plan.status != "open":
            return
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Cancelar este planejamento? As seleções serão desfeitas e nenhuma ação será executada.",
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

    def _clear_details(self) -> None:
        for value in self._details.values():
            value.setText("—")

    def _update_controls(self) -> None:
        plan = self._plan()
        has_row = 0 <= self._table.currentRow() < len(self._rows)
        open_plan = plan is not None and plan.status == "open"
        self._create_button.setEnabled(
            self._simulation is not None and self._context.operations is not None and not self._locked
        )
        self._approve_button.setEnabled(open_plan and has_row)
        self._reject_button.setEnabled(open_plan and has_row)
        self._cancel_plan_button.setEnabled(open_plan)
        for button in self._physical_buttons:
            button.setEnabled(False)  # operações reais bloqueadas nesta etapa
