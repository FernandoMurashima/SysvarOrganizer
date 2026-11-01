"""Diálogo de seleção de unidades de armazenamento."""

from __future__ import annotations

from PySide6.QtCore import QStorageInfo, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sysvar_organizer.ui.formatting import format_bytes
from sysvar_organizer.ui.widgets import label


class DriveSelectionDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Selecionar unidade de armazenamento")
        self.setMinimumSize(520, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(label("Unidades disponíveis", "SectionTitle"))
        layout.addWidget(
            label(
                "Marque uma ou mais unidades. A análise lê apenas nomes, tamanhos e "
                "datas; nenhum arquivo é alterado.",
                "BodyText",
                wrap=True,
            )
        )

        self._list = QListWidget()
        self._list.setObjectName("LocationList")
        for volume in QStorageInfo.mountedVolumes():
            if not volume.isValid() or not volume.isReady():
                continue
            root = volume.rootPath()
            name = volume.displayName() or root
            total = volume.bytesTotal()
            used = total - volume.bytesFree()
            text = f"{name}   ({root})   ·   {format_bytes(used)} usados de {format_bytes(total)}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, root)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._list.addItem(item)
        layout.addWidget(self._list, 1)

        if self._list.count() == 0:
            layout.addWidget(label("Nenhuma unidade disponível foi encontrada.", "BodyText"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Adicionar")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_paths(self) -> list[str]:
        paths = []
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                paths.append(item.data(Qt.ItemDataRole.UserRole))
        return paths
