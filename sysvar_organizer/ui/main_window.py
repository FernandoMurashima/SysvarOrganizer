"""Janela principal: menu lateral, área de páginas e barra de estado."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QCloseEvent, QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sysvar_organizer import APP_NAME, __version__
from sysvar_organizer.config import settings as keys
from sysvar_organizer.context import AppContext
from sysvar_organizer.core.errors import DatabaseError
from sysvar_organizer.ui.pages.dashboard import DashboardPage
from sysvar_organizer.ui.pages.storage import StoragePage
from sysvar_organizer.ui.pages.unavailable import UnavailablePage
from sysvar_organizer.ui.widgets import label, scrollable_page

logger = logging.getLogger(__name__)

SIDEBAR_WIDTH = 240
UNAVAILABLE_ITEM_COLOR = "#9CA3AF"


@dataclass(frozen=True)
class PageSpec:
    key: str
    title: str
    description: str
    available: bool = False


PAGES: tuple[PageSpec, ...] = (
    PageSpec("dashboard", "Dashboard", "Visão geral do aplicativo.", available=True),
    PageSpec(
        "storage",
        "Analisar Armazenamento",
        "Levantamento do uso de espaço em pastas e unidades escolhidas pelo usuário.",
        available=True,
    ),
    PageSpec(
        "duplicates",
        "Arquivos Duplicados",
        "Identificação de arquivos com conteúdo idêntico.",
    ),
    PageSpec(
        "compare",
        "Comparar Pastas",
        "Comparação do conteúdo entre pastas escolhidas pelo usuário.",
    ),
    PageSpec(
        "photos",
        "Fotografias Semelhantes",
        "Identificação de fotografias visualmente semelhantes.",
    ),
    PageSpec(
        "organization",
        "Organização",
        "Organização de arquivos segundo critérios definidos pelo usuário.",
    ),
    PageSpec(
        "history",
        "Histórico",
        "Consulta às análises e operações realizadas.",
    ),
    PageSpec(
        "settings",
        "Configurações",
        "Preferências do aplicativo.",
    ),
)


class MainWindow(QMainWindow):
    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self._context = context
        self.persist_geometry = True
        self._dashboard: DashboardPage | None = None
        self._storage: StoragePage | None = None

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 600)
        self.resize(1280, 800)

        central = QWidget()
        central.setObjectName("Central")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._nav = QListWidget()
        self._stack = QStackedWidget()
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._stack, 1)
        self.setCentralWidget(central)

        for spec in PAGES:
            self._add_page(spec)
        self._nav.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._stack.currentChanged.connect(self._on_page_changed)
        if self._dashboard is not None and self._storage is not None:
            self._storage.analysis_finished.connect(self._dashboard.refresh)
        self._nav.setCurrentRow(0)

        self._build_status_bar()
        self._restore_geometry()

    # Construção ---------------------------------------------------------

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        box = QVBoxLayout(sidebar)
        box.setContentsMargins(0, 24, 0, 16)
        box.setSpacing(0)

        brand = QVBoxLayout()
        brand.setContentsMargins(22, 0, 22, 20)
        brand.setSpacing(2)
        brand.addWidget(label("Sysvar Organizer", "SidebarTitle"))
        brand.addWidget(label("Organizador de arquivos", "SidebarSubtitle"))
        box.addLayout(brand)

        self._nav.setObjectName("NavList")
        self._nav.setFrameShape(QFrame.Shape.NoFrame)
        self._nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        box.addWidget(self._nav, 1)

        footer = label(f"Versão {__version__}", "SidebarFooter")
        footer.setContentsMargins(22, 8, 22, 0)
        box.addWidget(footer)
        return sidebar

    def _add_page(self, spec: PageSpec) -> None:
        item = QListWidgetItem(spec.title)
        item.setData(Qt.ItemDataRole.UserRole, spec.key)
        if not spec.available:
            item.setForeground(QColor(UNAVAILABLE_ITEM_COLOR))
            item.setToolTip("Indisponível nesta versão")
        self._nav.addItem(item)
        self._stack.addWidget(scrollable_page(self._create_page(spec)))

    def _create_page(self, spec: PageSpec) -> QWidget:
        if spec.key == "dashboard":
            self._dashboard = DashboardPage(self._context)
            return self._dashboard
        if spec.key == "storage":
            self._storage = StoragePage(self._context)
            return self._storage
        if spec.key == "settings":
            ctx = self._context
            return UnavailablePage(
                spec.title,
                spec.description,
                info_rows=[
                    ("Pasta de dados", str(ctx.data_dir)),
                    ("Arquivo do banco", str(ctx.database_path)),
                    ("Registro de eventos", str(ctx.log_path)),
                ],
            )
        return UnavailablePage(spec.title, spec.description)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(True)
        if self._context.database_ok:
            db_text = f"Banco local: operacional (esquema v{self._context.schema_version})"
        else:
            db_text = "Banco local: indisponível"
        bar.addWidget(QLabel(db_text), 1)
        bar.addPermanentWidget(QLabel(f"{APP_NAME} {__version__}"))

    # Navegação ----------------------------------------------------------

    def _on_page_changed(self, index: int) -> None:
        if self._dashboard is not None and PAGES[index].key == "dashboard":
            self._dashboard.refresh()

    def current_page_key(self) -> str:
        item = self._nav.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    # Geometria da janela ------------------------------------------------

    def _restore_geometry(self) -> None:
        settings = self._context.settings
        if settings is None:
            return
        try:
            encoded = settings.get(keys.MAIN_WINDOW_GEOMETRY)
        except DatabaseError:
            logger.exception("Falha ao ler a geometria da janela")
            return
        if isinstance(encoded, str) and encoded:
            self.restoreGeometry(QByteArray.fromBase64(QByteArray(encoded.encode("ascii"))))

    def _save_geometry(self) -> None:
        settings = self._context.settings
        if settings is None or not self.persist_geometry:
            return
        encoded = self.saveGeometry().toBase64().data().decode("ascii")
        try:
            settings.set(keys.MAIN_WINDOW_GEOMETRY, encoded)
        except DatabaseError:
            logger.exception("Falha ao gravar a geometria da janela")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - API do Qt
        if self._storage is not None and not self._storage.request_shutdown():
            event.ignore()
            return
        self._save_geometry()
        super().closeEvent(event)
