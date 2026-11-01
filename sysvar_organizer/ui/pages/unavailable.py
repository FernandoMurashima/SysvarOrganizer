"""Página para funcionalidades ainda não implementadas."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from sysvar_organizer.ui.widgets import (
    badge,
    card,
    field_grid,
    label,
    page_header,
    page_layout,
)


class UnavailablePage(QWidget):
    def __init__(
        self,
        title: str,
        description: str,
        info_rows: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        layout = page_layout(self)
        layout.addWidget(page_header(title, description, badge("Indisponível", "unavailable")))

        frame, box = card()
        box.addWidget(label("Funcionalidade não disponível nesta versão", "SectionTitle"))
        box.addWidget(
            label(
                "Este módulo ainda não foi implementado e será habilitado em etapa "
                "futura do projeto. Nenhuma ação é executada a partir desta tela.",
                "BodyText",
                wrap=True,
            )
        )
        layout.addWidget(frame)

        if info_rows:
            info, info_box = card()
            info_box.addWidget(label("Informações", "SectionTitle"))
            info_box.addSpacing(4)
            info_box.addLayout(field_grid(info_rows))
            layout.addWidget(info)

        layout.addStretch(1)
