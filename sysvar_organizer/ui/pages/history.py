"""Página Histórico: atividades registradas e planos de operação.

Duas seções separadas: atividades somente leitura (análises, buscas,
comparações e simulações) e planos de operação (estados, operações de teste,
falhas e reversões). A página apenas consulta o banco local.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPushButton, QTableWidgetItem, QWidget

from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.operations.models import (
    ACTION_LABELS,
    ENV_REAL,
    REAL_OPERATIONS_BLOCKED_MESSAGE,
    STATE_LABELS,
)
from sysvar_organizer.persistence.operations_store import PlanRecord
from sysvar_organizer.ui.formatting import format_count, format_timestamp
from sysvar_organizer.ui.widgets import Pager, badge, card, label, page_header, page_layout, result_table

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
STATUS_TEXT = {
    "completed": "Concluída",
    "cancelled": "Cancelada",
    "failed": "Falhou",
    "interrupted": "Interrompida",
    "running": "Em andamento",
}
LEVEL_TEXT = {"info": "Informação", "warning": "Atenção", "error": "Erro"}


def _item(text: str, *, tooltip: str | None = None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if tooltip:
        item.setToolTip(tooltip)
    return item


def environment_text(plan: PlanRecord) -> str:
    return "REAL (execução bloqueada)" if plan.environment == ENV_REAL else "TESTE (área isolada)"


class HistoryPage(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._plans: list[PlanRecord] = []

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Histórico",
                "Consulta às análises, buscas, simulações e planos de operação registrados "
                "no banco local.",
                badge("Somente consulta", "ok"),
            )
        )
        layout.addWidget(self._build_activities())
        layout.addWidget(self._build_plans())
        layout.addStretch(1)
        self.refresh()

    # Construção ---------------------------------------------------------

    def _build_activities(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Atividades somente leitura", "SectionTitle"))
        heading.addStretch(1)
        refresh = QPushButton("Atualizar")
        refresh.clicked.connect(self.refresh)
        heading.addWidget(refresh)
        box.addLayout(heading)
        box.addWidget(
            label(
                "Análises de armazenamento, buscas de duplicados, comparações de pastas, "
                "fotografias semelhantes e simulações de organização. Nenhuma delas altera arquivos.",
                "MetricHint",
                wrap=True,
            )
        )
        self._activities_table = result_table(
            ["Início", "Atividade", "Situação", "Objeto", "Resumo"], (3,)
        )
        self._activities_table.setMinimumHeight(220)
        box.addWidget(self._activities_table)
        self._activities_pager = Pager()
        self._activities_pager.page_changed.connect(self._load_activities)
        box.addWidget(self._activities_pager)
        return frame

    def _build_plans(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Planos e operações", "SectionTitle"))
        heading.addStretch(1)
        heading.addWidget(badge("Operações reais bloqueadas", "error"))
        box.addLayout(heading)
        box.addWidget(label(REAL_OPERATIONS_BLOCKED_MESSAGE, "MetricHint", wrap=True))
        self._plans_table = result_table(
            ["Plano", "Criado em", "Ambiente", "Operação", "Situação", "Ações", "Estados"], (6,)
        )
        self._plans_table.setMinimumHeight(180)
        box.addWidget(self._plans_table)

        box.addWidget(label("Eventos do plano", "FieldLabel"))
        self._plan_combo = QComboBox()
        self._plan_combo.setObjectName("AnalysisCombo")
        self._plan_combo.currentIndexChanged.connect(lambda *_: self._load_events(0))
        box.addWidget(self._plan_combo)
        self._events_table = result_table(["Data", "Nível", "Evento", "Mensagem", "Arquivo"], (3, 4))
        self._events_table.setMinimumHeight(200)
        box.addWidget(self._events_table)
        self._events_pager = Pager()
        self._events_pager.page_changed.connect(self._load_events)
        box.addWidget(self._events_pager)
        return frame

    # Dados --------------------------------------------------------------

    def refresh(self) -> None:
        self._load_activities(self._activities_pager.page)
        self._load_plans()

    def _load_activities(self, page: int) -> None:
        table, repo = self._activities_table, self._context.history_log
        table.setRowCount(0)
        if repo is None:
            self._activities_pager.set_total(0, PAGE_SIZE)
            return
        try:
            total = repo.count_activities()
            self._activities_pager.set_total(total, PAGE_SIZE, page)
            rows = repo.activities(self._activities_pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar o histórico de atividades")
            return
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            table.setItem(index, 0, _item(format_timestamp(row.started_at)))
            table.setItem(index, 1, _item(f"{row.kind} nº {row.id}"))
            table.setItem(index, 2, _item(STATUS_TEXT.get(row.status, row.status)))
            table.setItem(index, 3, _item(row.subject or "", tooltip=row.subject))
            table.setItem(index, 4, _item(row.summary or ""))

    def _load_plans(self) -> None:
        store = self._context.operations
        current = self._plan_combo.currentData()
        self._plans_table.setRowCount(0)
        self._plans = []
        counts: dict[int, dict[str, int]] = {}
        if store is not None:
            try:
                self._plans = store.plans()
                counts = {plan.id: store.state_counts(plan.id) for plan in self._plans}
            except DatabaseError:
                logger.exception("Falha ao consultar os planos de operação")
        self._plans_table.setRowCount(len(self._plans))
        for index, plan in enumerate(self._plans):
            states = "  ·  ".join(
                f"{STATE_LABELS.get(key, key)}: {format_count(value)}"
                for key, value in sorted(counts.get(plan.id, {}).items())
            )
            self._plans_table.setItem(index, 0, _item(f"nº {plan.id}"))
            self._plans_table.setItem(index, 1, _item(format_timestamp(plan.created_at)))
            self._plans_table.setItem(index, 2, _item(environment_text(plan)))
            self._plans_table.setItem(index, 3, _item(ACTION_LABELS.get(plan.operation, plan.operation)))
            self._plans_table.setItem(
                index, 4, _item("Cancelado" if plan.status == "cancelled" else "Aberto")
            )
            self._plans_table.setItem(index, 5, _item(format_count(plan.items_count)))
            self._plans_table.setItem(index, 6, _item(states, tooltip=states))

        self._plan_combo.blockSignals(True)
        self._plan_combo.clear()
        for plan in self._plans:
            self._plan_combo.addItem(
                f"Plano nº {plan.id}  ·  {environment_text(plan)}  ·  {format_timestamp(plan.created_at)}",
                plan.id,
            )
        if current is not None:
            found = self._plan_combo.findData(current)
            if found >= 0:
                self._plan_combo.setCurrentIndex(found)
        self._plan_combo.blockSignals(False)
        self._load_events(0)

    def _load_events(self, page: int) -> None:
        table, store = self._events_table, self._context.operations
        table.setRowCount(0)
        plan_id = self._plan_combo.currentData()
        if store is None or plan_id is None:
            self._events_pager.set_total(0, PAGE_SIZE)
            return
        try:
            total = store.count_events(plan_id)
            self._events_pager.set_total(total, PAGE_SIZE, page)
            rows = store.events(plan_id, self._events_pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar os eventos do plano")
            return
        table.setRowCount(len(rows))
        for index, (occurred, level, code, message, source) in enumerate(rows):
            table.setItem(index, 0, _item(format_timestamp(occurred)))
            item = _item(LEVEL_TEXT.get(level, level))
            if level == "error":
                item.setForeground(Qt.GlobalColor.darkRed)
            table.setItem(index, 1, item)
            table.setItem(index, 2, _item(code))
            table.setItem(index, 3, _item(message or "", tooltip=message))
            table.setItem(index, 4, _item(source or "—", tooltip=source))
