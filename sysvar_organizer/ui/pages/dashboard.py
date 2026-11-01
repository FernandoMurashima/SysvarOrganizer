"""Página inicial: identificação, indicadores, estado do sistema e operações."""

from __future__ import annotations

import logging
import platform
from datetime import datetime

import PySide6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget

from sysvar_organizer import APP_NAME, PROJECT_CODE, __version__
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.ui.widgets import (
    badge,
    card,
    field_grid,
    label,
    page_header,
    page_layout,
)

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "running": "em andamento",
    "completed": "concluída",
    "failed": "com falha",
    "cancelled": "cancelada",
}


class DashboardPage(QWidget):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context

        layout = page_layout(self)
        layout.addWidget(
            page_header(
                APP_NAME,
                f"Organizador de arquivos  ·  Projeto {PROJECT_CODE}  ·  Versão {__version__}",
            )
        )
        layout.addLayout(self._build_indicators())
        layout.addWidget(self._build_system_state())
        layout.addWidget(self._build_operations_area())
        layout.addStretch(1)

    # Indicadores --------------------------------------------------------

    def _build_indicators(self) -> QGridLayout:
        analyses_value, analyses_hint = "—", "Histórico indisponível"
        last_value, last_hint = "—", "Histórico indisponível"

        history = self._context.history
        if history is not None:
            try:
                total = history.count()
                latest = history.latest()
            except DatabaseError:
                logger.exception("Falha ao consultar o histórico de análises")
            else:
                analyses_value = str(total)
                analyses_hint = "Registradas no histórico local"
                if latest is None:
                    last_value, last_hint = "Nenhuma", "Nenhuma análise realizada"
                else:
                    last_value = _format_timestamp(latest.started_at)
                    last_hint = f"Situação: {_STATUS_LABELS.get(latest.status, latest.status)}"

        if self._context.database_ok:
            db_value = "Operacional"
            db_hint = f"Esquema versão {self._context.schema_version}"
        else:
            db_value = "Indisponível"
            db_hint = "Aplicativo sem persistência"

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        cards = [
            ("ANÁLISES REGISTRADAS", analyses_value, analyses_hint),
            ("ÚLTIMA ANÁLISE", last_value, last_hint),
            ("BANCO DE DADOS LOCAL", db_value, db_hint),
        ]
        for column, (title, value, hint) in enumerate(cards):
            frame, box = card()
            box.addWidget(label(title, "CardTitle"))
            box.addWidget(label(value, "MetricValue"))
            box.addWidget(label(hint, "MetricHint", wrap=True))
            grid.addWidget(frame, 0, column)
            grid.setColumnStretch(column, 1)
        return grid

    # Estado do sistema --------------------------------------------------

    def _build_system_state(self) -> QWidget:
        frame, box = card()
        heading = QHBoxLayout()
        heading.addWidget(label("Estado do sistema", "SectionTitle"))
        heading.addStretch(1)
        if self._context.database_ok:
            heading.addWidget(badge("Operacional", "ok"))
        else:
            heading.addWidget(badge("Banco indisponível", "error"))
        box.addLayout(heading)

        ctx = self._context
        if ctx.database_ok:
            db_state = f"Conectado  ·  esquema versão {ctx.schema_version}"
        else:
            db_state = f"Indisponível  ·  {ctx.database_error or 'erro desconhecido'}"

        rows = [
            ("Banco de dados", db_state),
            ("Arquivo do banco", str(ctx.database_path)),
            ("Pasta de dados", str(ctx.data_dir)),
            ("Registro de eventos", str(ctx.log_path)),
            ("Python", platform.python_version()),
            ("Qt / PySide6", f"{qVersion()} / {PySide6.__version__}"),
            (
                "Operações em arquivos",
                "Nenhuma habilitada. O aplicativo não lê, move nem exclui arquivos nesta versão.",
            ),
        ]
        box.addSpacing(4)
        box.addLayout(field_grid(rows))
        return frame

    # Operações ----------------------------------------------------------

    def _build_operations_area(self) -> QWidget:
        frame, box = card("ReservedArea")
        box.setContentsMargins(20, 20, 20, 24)
        heading = QHBoxLayout()
        heading.addWidget(label("Operações", "SectionTitle"))
        heading.addStretch(1)
        heading.addWidget(badge("Indisponível", "unavailable"))
        box.addLayout(heading)
        text = label(
            "Nenhuma operação disponível nesta versão. Análise de armazenamento, "
            "arquivos duplicados, comparação de pastas, fotografias semelhantes e "
            "organização serão habilitadas em etapas futuras.",
            "BodyText",
            wrap=True,
        )
        text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        box.addWidget(text)
        return frame


def _format_timestamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value
