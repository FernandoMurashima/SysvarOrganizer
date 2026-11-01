"""Componentes visuais reutilizados pelas páginas."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def label(text: str, object_name: str | None = None, *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    return widget


def badge(text: str, kind: str) -> QLabel:
    """Selo de estado. ``kind``: ``ok``, ``error`` ou ``unavailable``."""
    names = {"ok": "BadgeOk", "error": "BadgeError", "unavailable": "BadgeUnavailable"}
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
