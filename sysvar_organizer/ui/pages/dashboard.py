"""Página inicial: identificação, indicadores, estado do sistema e operações."""

from __future__ import annotations

import logging
import platform

import PySide6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget

from sysvar_organizer import APP_NAME, PROJECT_CODE, __version__
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.ui.formatting import (
    format_bytes,
    format_count,
    format_timestamp,
    status_label,
)
from sysvar_organizer.ui.widgets import (
    MetricCard,
    badge,
    card,
    field_grid,
    label,
    page_header,
    page_layout,
)

logger = logging.getLogger(__name__)


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
        self.refresh()

    # Indicadores --------------------------------------------------------

    def _build_indicators(self) -> QGridLayout:
        self._analyses_card = MetricCard("ANÁLISES REGISTRADAS")
        self._last_card = MetricCard("ÚLTIMA ANÁLISE")
        self._database_card = MetricCard("BANCO DE DADOS LOCAL")
        self._files_card = MetricCard("ARQUIVOS DA ÚLTIMA ANÁLISE CONCLUÍDA")
        self._volume_card = MetricCard("VOLUME TOTAL INDEXADO")

        # Grade de 6 colunas: 3 cartões na primeira linha e 2 na segunda.
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        grid.addWidget(self._analyses_card, 0, 0, 1, 2)
        grid.addWidget(self._last_card, 0, 2, 1, 2)
        grid.addWidget(self._database_card, 0, 4, 1, 2)
        grid.addWidget(self._files_card, 1, 0, 1, 3)
        grid.addWidget(self._volume_card, 1, 3, 1, 3)
        for column in range(6):
            grid.setColumnStretch(column, 1)
        return grid

    def refresh(self) -> None:
        """Atualiza os indicadores a partir do banco local."""
        if self._context.database_ok:
            self._database_card.set_value(
                "Operacional", f"Esquema versão {self._context.schema_version}"
            )
        else:
            self._database_card.set_value("Indisponível", "Aplicativo sem persistência")

        unavailable = "Histórico indisponível"
        history = self._context.history
        if history is None:
            for metric in (self._analyses_card, self._last_card, self._files_card, self._volume_card):
                metric.set_value("—", unavailable)
            return
        try:
            total = history.count()
            latest = history.latest()
            completed = history.latest_completed()
        except DatabaseError:
            logger.exception("Falha ao consultar o histórico de análises")
            for metric in (self._analyses_card, self._last_card, self._files_card, self._volume_card):
                metric.set_value("—", unavailable)
            return

        self._analyses_card.set_value(format_count(total), "Registradas no histórico local")

        if latest is None:
            self._last_card.set_value("Nenhuma", "Nenhuma análise realizada")
        else:
            self._last_card.set_value(
                format_timestamp(latest.started_at),
                f"Situação: {status_label(latest.status, latest.has_warnings).lower()}",
            )

        if completed is None:
            self._files_card.set_value("—", "Nenhuma análise concluída")
            self._volume_card.set_value("—", "Nenhuma análise concluída")
        else:
            self._files_card.set_value(
                format_count(completed.files_count),
                f"Em {format_count(completed.folders_count)} pastas  ·  "
                f"{format_timestamp(completed.started_at)}",
            )
            self._volume_card.set_value(
                format_bytes(completed.total_bytes),
                f"Última análise concluída  ·  {completed.target_path}",
            )

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
                "Somente leitura. Análises são executadas apenas quando solicitadas; "
                "nenhum arquivo é alterado, movido ou excluído.",
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
        heading.addWidget(badge("Somente leitura", "ok"))
        box.addLayout(heading)
        for text in (
            "Disponível: Analisar Armazenamento — varredura e indexação de pastas e "
            "unidades selecionadas, sem alterar arquivos.",
            "Indisponíveis nesta versão: arquivos duplicados, comparação de pastas, "
            "fotografias semelhantes e organização.",
        ):
            line = label(text, "BodyText", wrap=True)
            line.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            box.addWidget(line)
        return frame
