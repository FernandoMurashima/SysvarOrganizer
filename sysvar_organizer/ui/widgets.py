"""Componentes visuais reutilizados pelas páginas."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


class MetricCard(QFrame):
    """Cartão de indicador: título, valor e observação."""

    def __init__(self, title: str, object_name: str = "Card", value_name: str = "MetricValue"):
        super().__init__()
        self.setObjectName(object_name)
        box = QVBoxLayout(self)
        if object_name == "Card":
            box.setContentsMargins(20, 18, 20, 18)
        else:
            box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(6)
        box.addWidget(label(title, "CardTitle"))
        self._value = label("—", value_name)
        self._hint = label("", "MetricHint", wrap=True)
        box.addWidget(self._value)
        box.addWidget(self._hint)

    def set_value(self, value: str, hint: str = "") -> None:
        self._value.setText(value)
        self._hint.setText(hint)


class ElidedLabel(QLabel):
    """Rótulo de uma linha que abrevia o texto no meio quando não cabe."""

    def __init__(self, text: str = "", object_name: str | None = None) -> None:
        super().__init__()
        if object_name:
            self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._full_text = ""
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - API do Qt
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        width = max(self.width(), 40)
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, width
        )
        super().setText(elided)


def label(text: str, object_name: str | None = None, *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    return widget


def badge(text: str, kind: str) -> QLabel:
    """Selo de estado. ``kind``: ``ok``, ``error``, ``unavailable`` ou ``info``."""
    names = {
        "ok": "BadgeOk",
        "error": "BadgeError",
        "unavailable": "BadgeUnavailable",
        "info": "BadgeInfo",
    }
    widget = label(text, names[kind])
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return widget


def card(object_name: str = "Card") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(object_name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(8)
    return frame, layout


def page_header(title: str, subtitle: str, trailing: QWidget | None = None) -> QWidget:
    header = QWidget()
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    texts = QVBoxLayout()
    texts.setSpacing(2)
    texts.addWidget(label(title, "PageTitle"))
    texts.addWidget(label(subtitle, "PageSubtitle", wrap=True))
    row.addLayout(texts, 1)
    if trailing is not None:
        row.addWidget(trailing, 0, Qt.AlignmentFlag.AlignTop)
    return header


def field_grid(rows: list[tuple[str, str]]) -> QGridLayout:
    """Grade de pares rótulo/valor, com valores selecionáveis para cópia."""
    grid = QGridLayout()
    grid.setHorizontalSpacing(24)
    grid.setVerticalSpacing(8)
    grid.setColumnStretch(1, 1)
    for index, (name, value) in enumerate(rows):
        grid.addWidget(label(name, "FieldLabel"), index, 0, Qt.AlignmentFlag.AlignTop)
        value_label = label(value, "FieldValue", wrap=True)
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(value_label, index, 1)
    return grid


def scrollable_page(content: QWidget) -> QScrollArea:
    """Envolve a página em área com rolagem, para janelas de pouca altura."""
    content.setObjectName("Page")
    content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(content)
    return area


def page_layout(page: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout(page)
    layout.setContentsMargins(32, 28, 32, 28)
    layout.setSpacing(20)
    return layout


def result_table(headers: list[str], stretch_columns: tuple[int, ...]) -> QTableWidget:
    """Tabela somente leitura, uma linha por registro, seleção de linha única."""
    table = QTableWidget(0, len(headers))
    table.setObjectName("ResultTable")
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    header = table.horizontalHeader()
    for column in range(len(headers)):
        mode = (
            QHeaderView.ResizeMode.Stretch
            if column in stretch_columns
            else QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(column, mode)
    return table


class Pager(QWidget):
    """Navegação entre páginas de uma consulta paginada."""

    page_changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.page = 0
        self._pages = 1
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._info = label("", "MetricHint")
        self._previous = QPushButton("Anterior")
        self._next = QPushButton("Próxima")
        self._previous.clicked.connect(lambda: self._go(self.page - 1))
        self._next.clicked.connect(lambda: self._go(self.page + 1))
        row.addWidget(self._info)
        row.addStretch(1)
        row.addWidget(self._previous)
        row.addWidget(self._next)
        self.set_total(0, 1)

    def set_total(self, total: int, page_size: int, page: int = 0) -> None:
        self._pages = max(1, -(-total // page_size))
        self.page = min(max(page, 0), self._pages - 1)
        start = self.page * page_size + 1 if total else 0
        end = min(total, (self.page + 1) * page_size)
        self._info.setText(
            f"{start:,}–{end:,} de {total:,}  ·  página {self.page + 1} de {self._pages}".replace(
                ",", "."
            )
        )
        self._previous.setEnabled(self.page > 0)
        self._next.setEnabled(self.page < self._pages - 1)

    def _go(self, page: int) -> None:
        if 0 <= page < self._pages and page != self.page:
            self.page = page
            self.page_changed.emit(page)
