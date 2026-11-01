"""Tipos de valor dos relatórios e sua apresentação em CSV, JSON e na tela.

Valores ausentes permanecem ausentes (célula vazia, ``null`` ou "Não
registrado"): nada desconhecido é apresentado como se fosse conhecido.
"""

from __future__ import annotations

from datetime import datetime, timezone

TEXT = "text"
INT = "int"
BYTES = "bytes"
BOOL = "bool"
TIMESTAMP = "timestamp"  # texto ISO 8601 gravado pelo aplicativo (UTC)
EPOCH = "epoch"  # segundos Unix (datas dos arquivos indexados)
SECONDS = "seconds"  # duração em segundos (decimal)

NOT_RECORDED = "Não registrado"

# Planilhas interpretam células iniciadas por estes caracteres como fórmulas.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _local(dt: datetime) -> str:
    return dt.astimezone().strftime("%d/%m/%Y %H:%M:%S")


def timestamp_text(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _local(datetime.fromisoformat(value))
    except ValueError:
        return value


def epoch_text(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value).strftime("%d/%m/%Y %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def decimal_text(value: float | None) -> str | None:
    if value is None:
        return None
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def csv_cell(value, kind: str) -> str:
    """Texto de uma célula CSV (padrão brasileiro, compatível com Excel)."""
    if value is None:
        return ""
    if kind == BOOL:
        return "Sim" if value else "Não"
    if kind in (INT, BYTES):
        return str(int(value))
    if kind == TIMESTAMP:
        return timestamp_text(value) or ""
    if kind == EPOCH:
        return epoch_text(value) or ""
    if kind == SECONDS:
        return decimal_text(value) or ""
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        # Impede que nomes como "=SOMA(...)" ou "-relatorio" virem fórmulas.
        text = "'" + text
    return text


def json_value(value, kind: str):
    """Valor JSON fiel ao dado gravado (datas em ISO 8601)."""
    if value is None:
        return None
    if kind == BOOL:
        return bool(value)
    if kind in (INT, BYTES):
        return int(value)
    if kind == SECONDS:
        return round(float(value), 3)
    if kind == EPOCH:
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    return str(value) if kind == TIMESTAMP else value


def count_text(value: int | None) -> str:
    return NOT_RECORDED if value is None else f"{int(value):,}".replace(",", ".")


def bytes_text(value: int | None) -> str:
    if value is None:
        return NOT_RECORDED
    if value < 1024:
        return f"{value} bytes" if value != 1 else "1 byte"
    size = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        size /= 1024
        if size < 1024 or unit == "PB":
            human = f"{size:,.1f} {unit}".replace(",", "X").replace(".", ",").replace("X", ".")
            return f"{human} ({count_text(value)} bytes)"
    return f"{value} bytes"


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return NOT_RECORDED
    if seconds < 60:
        return f"{decimal_text(round(seconds, 1))} s"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours} h {minutes:02d} min {secs:02d} s" if hours else f"{minutes} min {secs:02d} s"


def display(value, kind: str) -> str:
    """Texto para a tela (detalhes)."""
    if value is None or value == "":
        return NOT_RECORDED
    if kind == BOOL:
        return "Sim" if value else "Não"
    if kind == INT:
        return count_text(value)
    if kind == BYTES:
        return bytes_text(value)
    if kind == TIMESTAMP:
        return timestamp_text(value) or NOT_RECORDED
    if kind == EPOCH:
        return epoch_text(value) or NOT_RECORDED
    if kind == SECONDS:
        return duration_text(value)
    return str(value)


def cell_text(value, kind: str) -> str:
    """Texto curto para células de tabela (uma linha por registro)."""
    if value is None or value == "":
        return "—"
    if kind == BYTES:
        return bytes_text(value).split(" (")[0]
    if kind == SECONDS:
        return duration_text(value)
    if kind == TIMESTAMP:
        text = timestamp_text(value)
        return text[:16] if text else "—"
    if kind == EPOCH:
        text = epoch_text(value)
        return text[:16] if text else "—"
    if kind == BOOL:
        return "Sim" if value else "Não"
    if kind == INT:
        return count_text(value)
    return " ".join(str(value).split())
