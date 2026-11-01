"""Folha de estilos da interface (tema claro)."""

from __future__ import annotations

STYLESHEET = """
QWidget {
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}
QLabel {
    color: #374151;
    background: transparent;
}

#Central, #Page {
    background: #F4F6FA;
}

/* Menu lateral */
#Sidebar {
    background: #111827;
}
#SidebarTitle {
    color: #F9FAFB;
    font-size: 17px;
    font-weight: 600;
}
#SidebarSubtitle {
    color: #9CA3AF;
    font-size: 11px;
}
#SidebarFooter {
    color: #6B7280;
    font-size: 11px;
}
#NavList {
    background: transparent;
    border: none;
    outline: 0;
    color: #E5E7EB;
    font-size: 13px;
}
#NavList::item {
    padding: 9px 12px;
    margin: 1px 10px;
    border-radius: 6px;
    border-left: 3px solid transparent;
}
#NavList::item:hover {
    background: #1F2937;
}
#NavList::item:selected {
    background: #1F2937;
    border-left: 3px solid #3B82F6;
    color: #FFFFFF;
}

/* Cabeçalho de página */
#PageTitle {
    color: #111827;
    font-size: 22px;
    font-weight: 600;
}
#PageSubtitle {
    color: #6B7280;
    font-size: 13px;
}

/* Cartões */
#Card {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
}
#CardTitle {
    color: #6B7280;
    font-size: 12px;
    font-weight: 600;
}
#MetricValue {
    color: #111827;
    font-size: 24px;
    font-weight: 600;
}
#MetricHint {
    color: #9CA3AF;
    font-size: 12px;
}
#SectionTitle {
    color: #111827;
    font-size: 15px;
    font-weight: 600;
}
#FieldLabel {
    color: #6B7280;
}
#FieldValue {
    color: #111827;
}
#BodyText {
    color: #4B5563;
}

/* Área reservada */
#ReservedArea {
    background: #F9FAFB;
    border: 1px dashed #D1D5DB;
    border-radius: 10px;
}

/* Selos de estado */
#BadgeUnavailable, #BadgeOk, #BadgeError {
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}
#BadgeUnavailable {
    background: #FEF3C7;
    color: #92400E;
}
#BadgeOk {
    background: #DCFCE7;
    color: #166534;
}
#BadgeError {
    background: #FEE2E2;
    color: #991B1B;
}

/* Indicadores compactos (página Analisar Armazenamento) */
#Tile {
    background: #F9FAFB;
    border: 1px solid #E5E7EB;
    border-radius: 8px;
}
#TileValue {
    color: #111827;
    font-size: 18px;
    font-weight: 600;
}

/* Botões */
QPushButton {
    background: #FFFFFF;
    color: #1F2937;
    border: 1px solid #D1D5DB;
    border-radius: 6px;
    padding: 7px 16px;
}
QPushButton:hover {
    background: #F3F4F6;
}
QPushButton:disabled {
    background: #F9FAFB;
    color: #9CA3AF;
    border-color: #E5E7EB;
}
#PrimaryButton {
    background: #2563EB;
    color: #FFFFFF;
    border: 1px solid #2563EB;
    font-weight: 600;
}
#PrimaryButton:hover {
    background: #1D4ED8;
}
#PrimaryButton:disabled {
    background: #BFDBFE;
    border-color: #BFDBFE;
    color: #FFFFFF;
}

/* Listas de locais */
#LocationList {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 8px;
    color: #1F2937;
    outline: 0;
}
#LocationList::item {
    padding: 6px 8px;
}
#LocationList::item:selected {
    background: #DBEAFE;
    color: #1E3A8A;
}

QProgressBar {
    background: #E5E7EB;
    border: none;
    border-radius: 3px;
}
QProgressBar::chunk {
    background: #2563EB;
    border-radius: 3px;
}

QDialog {
    background: #FFFFFF;
}

QScrollArea {
    background: transparent;
    border: none;
}
QStatusBar {
    background: #FFFFFF;
    border-top: 1px solid #E5E7EB;
    color: #6B7280;
}
QStatusBar QLabel {
    color: #6B7280;
    padding: 0 8px;
}
"""
