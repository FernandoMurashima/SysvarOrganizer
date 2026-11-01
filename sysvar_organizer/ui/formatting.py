"""Formatação de valores para exibição (padrão brasileiro)."""

from __future__ import annotations

from datetime import datetime

_STATUS_LABELS = {
    "running": "Em andamento",
    "completed": "Concluída",
    "cancelled": "Cancelada",
    "failed": "Falhou",
}


def format_count(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}".replace(",", ".")


def format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    if value < 1024:
        return f"{value} bytes" if value != 1 else "1 byte"
    size = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024 or unit == "PB":
            return f"{size:,.1f} {unit}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{value} bytes"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 10:
        return f"{seconds:.1f} s".replace(".", ",")
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def format_timestamp(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def status_label(status: str, has_warnings: bool = False) -> str:
    if status == "completed" and has_warnings:
        return "Concluída com avisos"
    return _STATUS_LABELS.get(status, status)
