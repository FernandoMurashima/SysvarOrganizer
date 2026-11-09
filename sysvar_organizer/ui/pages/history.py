"""Página Histórico: atividades registradas, planos de operação e relatórios.

Seções separadas: atividades somente leitura (análises, buscas, comparações e
simulações), detalhes da atividade ou do plano selecionado (com os resultados
associados e a exportação em CSV/JSON) e planos e operações (estados,
operações de teste, falhas e reversões). A página apenas consulta o banco
local; a única gravação possível é o arquivo de relatório, no caminho que o
usuário escolher.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QWidget,
)

from sysvar_organizer import APP_NAME
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.operations.models import (
    ACTION_LABELS,
    CONTROLLED_OPERATIONS_MESSAGE,
    STATE_LABELS,
)
from sysvar_organizer.persistence.history_repository import (
    ACTIVITY_LABELS,
    ActivityFilter,
    ActivityRecord,
)
from sysvar_organizer.persistence.operations_store import PlanRecord
from sysvar_organizer.reports.catalog import (
    KIND_ACTIVITIES,
    KIND_PLAN,
    STATUS_LABELS,
    ReportNotFound,
    activities_report,
    activity_summary,
    build_report,
    environment_label,
)
from sysvar_organizer.reports.export import (
    EXTENSIONS,
    FORMAT_CSV,
    FORMAT_JSON,
    ExportCancelled,
    ExportError,
    export_report,
)
from sysvar_organizer.reports.model import Report, count_rows, page_rows
from sysvar_organizer.reports.values import cell_text, display
from sysvar_organizer.ui.background import BackgroundTask
from sysvar_organizer.ui.formatting import format_count, format_timestamp
from sysvar_organizer.ui.widgets import (
    Pager,
    badge,
    card,
    label,
    page_header,
    page_layout,
    result_table,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
PLANS_PAGE_SIZE = 50
MAX_COLUMN_WIDTH = 360
LEVEL_TEXT = {"info": "Informação", "warning": "Atenção", "error": "Erro"}
PERIODS = (
    ("all", "Todo o período"),
    ("today", "Hoje"),
    ("7", "Últimos 7 dias"),
    ("30", "Últimos 30 dias"),
    ("365", "Últimos 12 meses"),
    ("custom", "Período personalizado"),
)


def _item(text: str, *, tooltip: str | None = None, right: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    if tooltip:
        item.setToolTip(tooltip)
    if right:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return item


def local_day_start_utc(day: datetime) -> str:
    """Meia-noite local do dia informado, convertida para UTC ISO 8601."""
    local = datetime.combine(day.date(), time.min).astimezone()
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def period_bounds(key: str, start: QDate | None = None, end: QDate | None = None):
    today = datetime.now()
    if key == "today":
        return local_day_start_utc(today), None
    if key in ("7", "30", "365"):
        return local_day_start_utc(today - timedelta(days=int(key) - 1)), None
    if key == "custom" and start is not None and end is not None:
        first = datetime(start.year(), start.month(), start.day())
        last = datetime(end.year(), end.month(), end.day()) + timedelta(days=1)
        return local_day_start_utc(first), local_day_start_utc(last)
    return None, None


def status_text(record: ActivityRecord) -> str:
    if record.status == "completed" and record.has_warnings:
        return "Concluída com avisos"
    return STATUS_LABELS.get(record.status, record.status)


class HistoryPage(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self._busy_check: Callable[[], bool] | None = None
        self._activities: list[ActivityRecord] = []
        self._plans: list[PlanRecord] = []
        self._report: Report | None = None
        self._selection: tuple[str, int] | None = None
        self._export = BackgroundTask(self)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(lambda: self._load_activities(0))

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                "Histórico",
                "Consulta às análises, buscas, simulações e planos de operação registrados "
                "no banco local, com exportação de relatórios em CSV e JSON.",
                badge("Somente consulta", "ok"),
            )
        )
        layout.addWidget(self._build_activities())
        self._details_frame = self._build_details()
        layout.addWidget(self._details_frame)
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
        if self._context.startup_notes:
            box.addWidget(
                label(
                    "Na inicialização: " + "; ".join(self._context.startup_notes) + ".",
                    "MetricHint",
                    wrap=True,
                )
            )

        filters = QHBoxLayout()
        self._kind_filter = QComboBox()
        self._kind_filter.setObjectName("AnalysisCombo")
        self._kind_filter.addItem("Todos os tipos de atividade", None)
        for key, text in ACTIVITY_LABELS.items():
            self._kind_filter.addItem(text, key)
        self._status_filter = QComboBox()
        self._status_filter.setObjectName("AnalysisCombo")
        self._status_filter.addItem("Todas as situações", None)
        for key, text in STATUS_LABELS.items():
            self._status_filter.addItem(text, key)
        self._period_filter = QComboBox()
        self._period_filter.setObjectName("AnalysisCombo")
        for key, text in PERIODS:
            self._period_filter.addItem(text, key)
        self._order = QComboBox()
        self._order.setObjectName("AnalysisCombo")
        self._order.addItem("Mais recentes primeiro", True)
        self._order.addItem("Mais antigas primeiro", False)
        for combo in (self._kind_filter, self._status_filter, self._period_filter, self._order):
            combo.currentIndexChanged.connect(self._on_filter_changed)
            filters.addWidget(combo)
        filters.addStretch(1)
        box.addLayout(filters)

        search = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setObjectName("TextInput")
        self._search.setPlaceholderText("Pesquisar por nome, caminho ou número da atividade")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda *_: self._search_timer.start())
        search.addWidget(self._search, 1)
        self._date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self._date_to = QDateEdit(QDate.currentDate())
        for edit in (self._date_from, self._date_to):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("dd/MM/yyyy")
            edit.dateChanged.connect(self._on_filter_changed)
        self._date_label = label("até", "MetricHint")
        search.addWidget(self._date_from)
        search.addWidget(self._date_label)
        search.addWidget(self._date_to)
        clear = QPushButton("Limpar filtros")
        clear.clicked.connect(self._clear_filters)
        search.addWidget(clear)
        box.addLayout(search)

        self._activities_table = result_table(
            ["Início", "Atividade", "Situação", "Origem", "Resumo"], (3,)
        )
        self._activities_table.setMinimumHeight(240)
        self._activities_table.itemSelectionChanged.connect(self._on_activity_selected)
        box.addWidget(self._activities_table)
        self._activities_pager = Pager()
        self._activities_pager.page_changed.connect(self._load_activities)
        box.addWidget(self._activities_pager)

        export = QHBoxLayout()
        self._scope_label = label("", "MetricHint")
        export.addWidget(self._scope_label, 1)
        self._list_csv = QPushButton("Exportar lista (CSV)")
        self._list_json = QPushButton("Exportar lista (JSON)")
        self._list_csv.clicked.connect(lambda: self._export_activities(FORMAT_CSV))
        self._list_json.clicked.connect(lambda: self._export_activities(FORMAT_JSON))
        export.addWidget(self._list_csv)
        export.addWidget(self._list_json)
        box.addLayout(export)
        self._update_period_controls()
        return frame

    def _build_details(self) -> QWidget:
        frame, box = card()
        self._details_title = label("Detalhes", "SectionTitle")
        box.addWidget(self._details_title)
        self._details_hint = label(
            "Selecione uma atividade ou um plano para ver os dados consolidados e os "
            "resultados associados.",
            "MetricHint",
            wrap=True,
        )
        box.addWidget(self._details_hint)
        self._notices = label("", "BodyText", wrap=True)
        box.addWidget(self._notices)
        self._summary_grid = QGridLayout()
        self._summary_grid.setHorizontalSpacing(24)
        self._summary_grid.setVerticalSpacing(4)
        self._summary_grid.setColumnStretch(1, 1)
        box.addLayout(self._summary_grid)

        box.addSpacing(6)
        box.addWidget(label("Resultados associados", "FieldLabel"))
        self._dataset_combo = QComboBox()
        self._dataset_combo.setObjectName("AnalysisCombo")
        self._dataset_combo.currentIndexChanged.connect(lambda *_: self._load_dataset(0))
        box.addWidget(self._dataset_combo)
        self._dataset_table = result_table(["—"], ())
        self._dataset_table.setMinimumHeight(220)
        box.addWidget(self._dataset_table)
        self._dataset_pager = Pager()
        self._dataset_pager.page_changed.connect(self._load_dataset)
        box.addWidget(self._dataset_pager)

        export = QHBoxLayout()
        self._export_status = label("", "MetricHint")
        export.addWidget(self._export_status, 1)
        self._csv_button = QPushButton("Exportar CSV (conjunto exibido)")
        self._json_button = QPushButton("Exportar JSON (relatório completo)")
        self._cancel_export = QPushButton("Cancelar exportação")
        self._csv_button.clicked.connect(lambda: self._export_selected(FORMAT_CSV))
        self._json_button.clicked.connect(lambda: self._export_selected(FORMAT_JSON))
        self._cancel_export.clicked.connect(self._export.cancel)
        for button in (self._csv_button, self._json_button, self._cancel_export):
            export.addWidget(button)
        box.addLayout(export)
        box.addWidget(
            label(
                "Os relatórios usam somente os dados já gravados; nada é analisado novamente "
                "e nenhum arquivo analisado é alterado. Um arquivo existente só é substituído "
                "após confirmação.",
                "MetricHint",
                wrap=True,
            )
        )
        return frame

    def _build_plans(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Planos e operações", "SectionTitle"))
        heading.addStretch(1)
        heading.addWidget(badge("Operações reais só em áreas autorizadas", "unavailable"))
        box.addLayout(heading)
        box.addWidget(label(CONTROLLED_OPERATIONS_MESSAGE, "MetricHint", wrap=True))
        self._plans_table = result_table(
            ["Plano", "Criado em", "Ambiente", "Operação", "Situação", "Ações", "Estados"], (6,)
        )
        self._plans_table.setMinimumHeight(180)
        self._plans_table.itemSelectionChanged.connect(self._on_plan_selected)
        box.addWidget(self._plans_table)
        self._plans_pager = Pager()
        self._plans_pager.page_changed.connect(self._load_plans)
        box.addWidget(self._plans_pager)

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

    # Tarefas ------------------------------------------------------------

    def set_busy_check(self, check: Callable[[], bool]) -> None:
        self._busy_check = check

    def is_running(self) -> bool:
        return self._export.is_running()

    def request_shutdown(self) -> bool:
        if not self.is_running():
            return True
        answer = QMessageBox.question(
            self,
            APP_NAME,
            "Uma exportação de relatório está em andamento. Deseja cancelá-la e sair? "
            "O arquivo parcial será descartado.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if not self._export.wait(15000):
            logger.warning("A exportação não terminou no tempo de espera do encerramento")
        return True

    # Filtros ------------------------------------------------------------

    def current_filter(self) -> ActivityFilter:
        start, end = period_bounds(
            self._period_filter.currentData(), self._date_from.date(), self._date_to.date()
        )
        return ActivityFilter(
            kind=self._kind_filter.currentData(),
            status=self._status_filter.currentData(),
            text=str(self._search.text()),
            start=start,
            end=end,
            newest_first=bool(self._order.currentData()),
        )

    def _on_filter_changed(self, *_args) -> None:
        self._update_period_controls()
        self._load_activities(0)

    def _update_period_controls(self) -> None:
        custom = self._period_filter.currentData() == "custom"
        for widget in (self._date_from, self._date_label, self._date_to):
            widget.setVisible(custom)

    def _clear_filters(self) -> None:
        for combo in (self._kind_filter, self._status_filter, self._period_filter, self._order):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._update_period_controls()
        self._load_activities(0)

    # Dados --------------------------------------------------------------

    def refresh(self) -> None:
        self._load_activities(self._activities_pager.page)
        self._load_plans(self._plans_pager.page)
        self._update_controls()

    def _load_activities(self, page: int) -> None:
        table, repo = self._activities_table, self._context.history_log
        table.setRowCount(0)
        self._activities = []
        if repo is None:
            self._activities_pager.set_total(0, PAGE_SIZE)
            self._scope_label.setText("Banco de dados indisponível.")
            self._update_controls()
            return
        filters = self.current_filter()
        try:
            total = repo.count_activities(filters)
            all_total = repo.count_activities() if filters.is_filtered else total
            self._activities_pager.set_total(total, PAGE_SIZE, page)
            self._activities = repo.activities(
                self._activities_pager.page * PAGE_SIZE, PAGE_SIZE, filters
            )
        except DatabaseError:
            logger.exception("Falha ao consultar o histórico de atividades")
            self._scope_label.setText("Não foi possível consultar o histórico.")
            self._update_controls()
            return
        self._scope_label.setText(
            f"A exportação inclui somente os registros filtrados: {format_count(total)} de "
            f"{format_count(all_total)} (todas as páginas)."
            if filters.is_filtered
            else f"A exportação inclui todos os registros: {format_count(total)} (todas as páginas)."
        )
        table.setRowCount(len(self._activities))
        for index, record in enumerate(self._activities):
            summary = activity_summary(record.kind, record.m1, record.m2, record.m3) or "—"
            table.setItem(index, 0, _item(format_timestamp(record.started_at)))
            table.setItem(index, 1, _item(f"{record.label} nº {record.id}"))
            table.setItem(index, 2, _item(status_text(record), tooltip=record.error_message))
            table.setItem(index, 3, _item(record.subject or "—", tooltip=record.subject))
            table.setItem(index, 4, _item(summary, tooltip=summary))
        self._update_controls()

    def _load_plans(self, page: int = 0) -> None:
        store = self._context.operations
        current = self._plan_combo.currentData()
        self._plans_table.setRowCount(0)
        self._plans = []
        counts: dict[int, dict[str, int]] = {}
        if store is not None:
            try:
                total = store.count_plans()
                self._plans_pager.set_total(total, PLANS_PAGE_SIZE, page)
                self._plans = store.plans(self._plans_pager.page * PLANS_PAGE_SIZE, PLANS_PAGE_SIZE)
                counts = store.state_counts_for([plan.id for plan in self._plans])
            except DatabaseError:
                logger.exception("Falha ao consultar os planos de operação")
        else:
            self._plans_pager.set_total(0, PLANS_PAGE_SIZE)
        self._plans_table.setRowCount(len(self._plans))
        for index, plan in enumerate(self._plans):
            states = "  ·  ".join(
                f"{STATE_LABELS.get(key, key)}: {format_count(value)}"
                for key, value in sorted(counts.get(plan.id, {}).items())
            )
            self._plans_table.setItem(index, 0, _item(f"nº {plan.id}"))
            self._plans_table.setItem(index, 1, _item(format_timestamp(plan.created_at)))
            self._plans_table.setItem(index, 2, _item(environment_label(plan.environment, plan.area_id)))
            self._plans_table.setItem(
                index, 3, _item(ACTION_LABELS.get(plan.operation, plan.operation))
            )
            self._plans_table.setItem(
                index, 4, _item("Cancelado" if plan.status == "cancelled" else "Aberto")
            )
            self._plans_table.setItem(index, 5, _item(format_count(plan.items_count), right=True))
            self._plans_table.setItem(index, 6, _item(states, tooltip=states))

        self._plan_combo.blockSignals(True)
        self._plan_combo.clear()
        for plan in self._plans:
            self._plan_combo.addItem(
                f"Plano nº {plan.id}  ·  {environment_label(plan.environment, plan.area_id)}  ·  "
                f"{format_timestamp(plan.created_at)}",
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

    # Seleção e detalhes -------------------------------------------------

    def _on_activity_selected(self) -> None:
        index = self._activities_table.currentRow()
        if 0 <= index < len(self._activities):
            record = self._activities[index]
            self._show_report(record.kind, record.id)

    def _on_plan_selected(self) -> None:
        index = self._plans_table.currentRow()
        if 0 <= index < len(self._plans):
            plan = self._plans[index]
            found = self._plan_combo.findData(plan.id)
            if found >= 0:
                self._plan_combo.setCurrentIndex(found)
            self._show_report(KIND_PLAN, plan.id)
            self._reveal_details()

    def _reveal_details(self) -> None:
        widget = self.parentWidget()
        for _level in range(10):  # a página fica poucos níveis abaixo da área de rolagem
            if widget is None or isinstance(widget, QScrollArea):
                break
            widget = widget.parentWidget()
        if isinstance(widget, QScrollArea):
            widget.ensureWidgetVisible(self._details_frame, 0, 0)

    def _show_report(self, kind: str, source_id: int) -> None:
        database = self._context.database
        self._report = None
        self._selection = (kind, source_id)
        if database is None:
            return
        try:
            with database.connection() as conn:
                self._report = build_report(conn, kind, source_id)
        except (DatabaseError, ReportNotFound) as exc:
            logger.warning("Detalhes indisponíveis (tipo=%s, id=%s): %s", kind, source_id, exc)
            self._details_title.setText("Detalhes")
            self._details_hint.setText(str(exc))
            self._clear_summary()
            self._dataset_combo.clear()
            self._update_controls()
            return
        report = self._report
        self._details_title.setText(report.title)
        self._details_hint.setText(report.scope)
        self._notices.setText("\n".join(f"• {text}" for text in report.notices))
        self._clear_summary()
        for row, item in enumerate(report.summary):
            self._summary_grid.addWidget(
                label(item.label, "FieldLabel"), row, 0, Qt.AlignmentFlag.AlignTop
            )
            value = label(display(item.value, item.kind), "FieldValue", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._summary_grid.addWidget(value, row, 1)
        self._dataset_combo.blockSignals(True)
        self._dataset_combo.clear()
        for dataset in report.datasets:
            self._dataset_combo.addItem(dataset.title, dataset.key)
        self._dataset_combo.blockSignals(False)
        self._load_dataset(0)
        self._update_controls()

    def _clear_summary(self) -> None:
        while self._summary_grid.count():
            entry = self._summary_grid.takeAt(0)
            widget = entry.widget() if entry is not None else None
            if widget is not None:
                widget.deleteLater()

    def _load_dataset(self, page: int) -> None:
        table, report, database = self._dataset_table, self._report, self._context.database
        table.setRowCount(0)
        key = self._dataset_combo.currentData()
        if report is None or database is None or key is None:
            self._dataset_pager.set_total(0, PAGE_SIZE)
            return
        try:
            dataset = report.dataset(key)
        except KeyError:
            self._dataset_pager.set_total(0, PAGE_SIZE)
            return
        try:
            with database.connection() as conn:
                total = count_rows(conn, dataset)
                self._dataset_pager.set_total(total, PAGE_SIZE, page)
                rows = page_rows(conn, dataset, self._dataset_pager.page * PAGE_SIZE, PAGE_SIZE)
        except DatabaseError:
            logger.exception("Falha ao consultar os resultados associados")
            return
        table.setColumnCount(len(dataset.columns))
        table.setHorizontalHeaderLabels([c.header for c in dataset.columns])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, (value, spec) in enumerate(zip(row, dataset.columns)):
                text = cell_text(value, spec.kind)
                table.setItem(index, column, _item(text, tooltip=text if len(text) > 40 else None))
        table.resizeColumnsToContents()
        for column in range(len(dataset.columns)):
            if table.columnWidth(column) > MAX_COLUMN_WIDTH:
                table.setColumnWidth(column, MAX_COLUMN_WIDTH)

    # Exportação ---------------------------------------------------------

    def _export_activities(self, fmt: str) -> None:
        filters = self.current_filter()
        suffix = "filtrado" if filters.is_filtered else "completo"
        self._start_export(KIND_ACTIVITIES, None, fmt, None, f"historico-{suffix}", filters)

    def _export_selected(self, fmt: str) -> None:
        if self._report is None or self._selection is None:
            return
        kind, source_id = self._selection
        dataset = self._dataset_combo.currentData() if fmt == FORMAT_CSV else None
        if fmt == FORMAT_CSV and dataset is None:
            return
        base = f"{kind}-{source_id}" + (f"-{dataset}" if dataset else "")
        self._start_export(kind, source_id, fmt, dataset, base, None)

    def _start_export(
        self,
        kind: str,
        source_id: int | None,
        fmt: str,
        dataset: str | None,
        base: str,
        filters: ActivityFilter | None,
    ) -> None:
        database = self._context.database
        if database is None or self.is_running():
            return
        if self._busy_check is not None and self._busy_check():
            QMessageBox.information(
                self, APP_NAME, "Há outra tarefa em andamento. Aguarde a conclusão para exportar."
            )
            return
        extension = EXTENSIONS[fmt]
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        folder = Path.home() / "Documents"
        folder = folder if folder.is_dir() else Path.home()
        suggested = str(folder / f"sysvar-organizer-{base}-{stamp}{extension}")
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Exportar relatório {fmt.upper()}",
            suggested,
            f"{fmt.upper()} (*{extension})",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != extension:
            destination = destination.with_name(destination.name + extension)
        overwrite = False
        if os.path.lexists(destination):
            answer = QMessageBox.question(
                self,
                APP_NAME,
                f"Já existe um arquivo com este nome:\n{destination}\n\nDeseja substituí-lo pelo "
                "novo relatório? O arquivo atual só será trocado depois que o novo estiver "
                "completamente gravado.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            overwrite = True

        def task(cancel, progress):
            conn = database.open()
            try:
                if kind == KIND_ACTIVITIES:
                    report = activities_report(conn, filters or ActivityFilter())
                    key = "atividades" if fmt == FORMAT_CSV else None
                else:
                    report = build_report(conn, kind, source_id)
                    key = dataset
                return export_report(
                    conn,
                    report,
                    destination,
                    fmt,
                    dataset_key=key,
                    overwrite=overwrite,
                    cancel=cancel,
                    progress=progress,
                )
            finally:
                conn.close()

        self._export_status.setText("Exportando…")
        self._export.start(
            task, self._on_export_finished, self._on_export_progress, self._update_controls
        )
        self._update_controls()

    def _on_export_progress(self, done: int, total: int) -> None:
        self._export_status.setText(
            f"Exportando: {format_count(done)} de {format_count(total)} registros…"
        )

    def _on_export_finished(self, result, error) -> None:
        if error is None:
            self._export_status.setText(
                f"Relatório gravado: {format_count(result.rows)} registros em {result.path}"
            )
            QMessageBox.information(
                self,
                APP_NAME,
                f"Relatório gravado com {format_count(result.rows)} registros.\n\n{result.path}",
            )
            return
        if isinstance(error, ExportCancelled):
            self._export_status.setText("Exportação cancelada. Nenhum arquivo foi gravado.")
            return
        if isinstance(error, (ExportError, DatabaseError, ReportNotFound)):
            message = str(error)
        else:
            logger.error("Falha inesperada na exportação", exc_info=error)
            message = "Não foi possível exportar o relatório. Detalhes no log do aplicativo."
        self._export_status.setText("Exportação não concluída.")
        QMessageBox.warning(self, APP_NAME, message)

    def _update_controls(self) -> None:
        running = self._export.is_running()
        available = self._context.database is not None
        has_report = self._report is not None
        self._list_csv.setEnabled(available and not running)
        self._list_json.setEnabled(available and not running)
        self._csv_button.setEnabled(
            available and has_report and not running and self._dataset_combo.count() > 0
        )
        self._json_button.setEnabled(available and has_report and not running)
        self._cancel_export.setEnabled(running)
