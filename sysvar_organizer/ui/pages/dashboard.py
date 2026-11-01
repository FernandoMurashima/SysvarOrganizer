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
        self._groups_card = MetricCard("GRUPOS DE DUPLICADOS")
        self._excess_card = MetricCard("CÓPIAS EXCEDENTES")
        self._recoverable_card = MetricCard("ESPAÇO POTENCIALMENTE RECUPERÁVEL")
        self._comparison_card = MetricCard("ÚLTIMA COMPARAÇÃO DE PASTAS")
        self._cmp_identical_card = MetricCard("IDÊNTICOS (COMPARAÇÃO)")
        self._cmp_different_card = MetricCard("DIFERENTES (COMPARAÇÃO)")
        self._cmp_exclusive_card = MetricCard("EXCLUSIVOS (COMPARAÇÃO)")
        self._cmp_shared_card = MetricCard("VOLUME LÓGICO COMPARTILHADO")

        # Grade de 6 colunas: 3 cartões na primeira linha, 2 na segunda
        # (armazenamento) e 3 na terceira (duplicados exatos).
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        grid.addWidget(self._analyses_card, 0, 0, 1, 2)
        grid.addWidget(self._last_card, 0, 2, 1, 2)
        grid.addWidget(self._database_card, 0, 4, 1, 2)
        grid.addWidget(self._files_card, 1, 0, 1, 3)
        grid.addWidget(self._volume_card, 1, 3, 1, 3)
        grid.addWidget(self._groups_card, 2, 0, 1, 2)
        grid.addWidget(self._excess_card, 2, 2, 1, 2)
        grid.addWidget(self._recoverable_card, 2, 4, 1, 2)
        grid.addWidget(self._comparison_card, 3, 0, 1, 2)
        grid.addWidget(self._cmp_identical_card, 3, 2, 1, 2)
        grid.addWidget(self._cmp_different_card, 3, 4, 1, 2)
        grid.addWidget(self._cmp_exclusive_card, 4, 0, 1, 3)
        grid.addWidget(self._cmp_shared_card, 4, 3, 1, 3)
        for column in range(6):
            grid.setColumnStretch(column, 1)
        return grid

    def refresh(self) -> None:
        """Atualiza os indicadores a partir do banco local."""
        self._refresh_duplicates()
        self._refresh_comparison()
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

    def _refresh_duplicates(self) -> None:
        cards = (self._groups_card, self._excess_card, self._recoverable_card)
        repo = self._context.duplicates
        if repo is None:
            for metric in cards:
                metric.set_value("—", "Histórico indisponível")
            return
        try:
            search = repo.latest_completed()
        except DatabaseError:
            logger.exception("Falha ao consultar a última busca de duplicados")
            for metric in cards:
                metric.set_value("—", "Histórico indisponível")
            return
        if search is None:
            for metric in cards:
                metric.set_value("—", "Nenhuma busca de duplicados concluída")
            return
        when = format_timestamp(search.started_at)
        self._groups_card.set_value(
            format_count(search.groups_count), f"Duplicados exatos  ·  busca de {when}"
        )
        self._excess_card.set_value(
            format_count(search.excess_copies), "Cópias além de uma por grupo"
        )
        self._recoverable_card.set_value(
            format_bytes(search.recoverable_bytes),
            "Estimativa lógica; não é economia garantida em disco",
        )

    def _refresh_comparison(self) -> None:
        cards = (
            self._comparison_card,
            self._cmp_identical_card,
            self._cmp_different_card,
            self._cmp_exclusive_card,
            self._cmp_shared_card,
        )
        repo = self._context.comparisons
        record = None
        unavailable = "Histórico indisponível"
        if repo is not None:
            try:
                record = repo.latest_completed()
                unavailable = "Nenhuma comparação de pastas concluída"
            except DatabaseError:
                logger.exception("Falha ao consultar a última comparação")
        if record is None:
            for metric in cards:
                metric.set_value("—", unavailable)
            return
        self._comparison_card.set_value(
            format_timestamp(record.started_at), f"A: {record.root_a}\nB: {record.root_b}"
        )
        self._cmp_identical_card.set_value(
            format_count((record.identical_pairs or 0) + (record.renamed_pairs or 0)),
            f"Correspondências de conteúdo  ·  {format_count(record.identical_pairs)} no mesmo "
            "caminho",
        )
        self._cmp_different_card.set_value(
            format_count(record.different_count), "Mesmo caminho, conteúdo diferente"
        )
        self._cmp_exclusive_card.set_value(
            format_count((record.only_a_count or 0) + (record.only_b_count or 0)),
            f"A: {format_count(record.only_a_count)}  ·  B: {format_count(record.only_b_count)}"
            f"  ·  {format_count(record.indeterminate_count)} indeterminados",
        )
        self._cmp_shared_card.set_value(
            format_bytes(record.shared_bytes),
            f"{format_count(record.shared_contents)} conteúdos presentes nos dois lados",
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
                "Somente leitura. Análises e buscas são executadas apenas quando "
                "solicitadas; nenhum arquivo é alterado, movido ou excluído.",
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
            "Disponível: Arquivos Duplicados — identificação de duplicados exatos por "
            "SHA-256, somente para consulta.",
            "Disponível: Comparar Pastas — comparação informativa entre duas pastas "
            "indexadas, sem sincronizar, copiar ou mover arquivos.",
            "Indisponíveis nesta versão: fotografias semelhantes e organização.",
        ):
            line = label(text, "BodyText", wrap=True)
            line.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            box.addWidget(line)
        return frame
