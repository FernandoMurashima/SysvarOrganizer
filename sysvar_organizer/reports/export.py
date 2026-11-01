"""Exportação de relatórios em CSV e JSON com gravação segura.

A gravação é a ÚNICA escrita feita pelos relatórios, e só no caminho que o
usuário escolheu:

* o conteúdo vai para um temporário exclusivo na pasta de destino e só é
  finalizado depois de gravado por completo (flush + fsync);
* destino existente nunca é substituído sem confirmação explícita
  (``overwrite=True``); mesmo assim, só é substituído um arquivo comum com a
  mesma extensão do relatório e que não seja um arquivo indexado nas análises;
* qualquer falha ou cancelamento remove o temporário e preserva o destino.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import secrets
import sqlite3
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from sysvar_organizer import APP_NAME, __version__
from sysvar_organizer.reports.model import FORMAT_VERSION, Report, count_rows, iterate_rows
from sysvar_organizer.reports.values import csv_cell, json_value

logger = logging.getLogger(__name__)

FORMAT_CSV = "csv"
FORMAT_JSON = "json"
EXTENSIONS = {FORMAT_CSV: ".csv", FORMAT_JSON: ".json"}
CSV_DELIMITER = ";"
PROGRESS_EVERY = 1000

Progress = Callable[[int, int], None]


class ExportError(Exception):
    code = "export_failed"


class DestinationExists(ExportError):
    code = "destination_exists"


class DestinationRefused(ExportError):
    code = "destination_refused"


class ExportCancelled(ExportError):
    code = "cancelled"


@dataclass(frozen=True)
class ExportResult:
    path: str
    format: str
    rows: int
    bytes_written: int
    elapsed_seconds: float
    replaced: bool


def indexed_file_at(conn: sqlite3.Connection, path: Path) -> bool:
    """O caminho corresponde a um arquivo indexado em alguma análise?"""
    folder, name = str(path.parent), path.name
    row = conn.execute(
        """
        SELECT 1 FROM indexed_files f JOIN indexed_folders d ON d.id = f.folder_id
        WHERE d.path = ? COLLATE NOCASE AND f.name = ? COLLATE NOCASE LIMIT 1
        """,
        (folder, name),
    ).fetchone()
    return row is not None


def check_destination(
    conn: sqlite3.Connection, destination: Path, fmt: str, overwrite: bool
) -> bool:
    """Valida o destino. Retorna ``True`` se ele já existe (substituição)."""
    if not destination.is_absolute():
        raise DestinationRefused("Informe um caminho completo para o relatório.")
    if destination.suffix.lower() != EXTENSIONS[fmt]:
        raise DestinationRefused(f"O relatório {fmt.upper()} deve ter a extensão {EXTENSIONS[fmt]}.")
    parent = destination.parent
    if not parent.is_dir():
        raise DestinationRefused("A pasta escolhida para o relatório não existe.")
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DestinationRefused("Não foi possível verificar o destino do relatório.") from exc
    if not stat.S_ISREG(info.st_mode) or _is_link_like(info):
        raise DestinationRefused(
            "Já existe no destino um item que não é um arquivo comum; ele não será substituído."
        )
    if indexed_file_at(conn, destination):
        raise DestinationRefused(
            "O destino é um arquivo incluído em uma análise. Relatórios nunca substituem "
            "arquivos analisados; escolha outro nome."
        )
    if not overwrite:
        raise DestinationExists("Já existe um arquivo com este nome no destino.")
    return True


def _is_link_like(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_reparse_tag", 0))


def export_report(
    conn: sqlite3.Connection,
    report: Report,
    destination: Path,
    fmt: str,
    *,
    dataset_key: str | None = None,
    overwrite: bool = False,
    cancel: Event | None = None,
    progress: Progress | None = None,
) -> ExportResult:
    """Exporta ``report``. CSV grava um conjunto de dados (``dataset_key``);
    JSON grava metadados, resumo, avisos e todos os conjuntos."""
    if fmt not in EXTENSIONS:
        raise ExportError(f"Formato desconhecido: {fmt}")
    if fmt == FORMAT_CSV and dataset_key is None:
        raise ExportError("Escolha o conjunto de dados a exportar em CSV.")
    started = time.monotonic()
    replaced = check_destination(conn, destination, fmt, overwrite)
    datasets = [report.dataset(dataset_key)] if fmt == FORMAT_CSV else list(report.datasets)
    total = sum(count_rows(conn, d) for d in datasets)
    temp = destination.parent / f".{destination.name}.{secrets.token_hex(6)}.tmp"
    rows = 0
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        with os.fdopen(fd, "wb") as raw:
            if fmt == FORMAT_CSV:
                rows = _write_csv(raw, conn, datasets[0], total, cancel, progress)
            else:
                rows = _write_json(raw, conn, report, datasets, total, cancel, progress)
            raw.flush()
            os.fsync(raw.fileno())
        size = os.path.getsize(temp)
        if _cancelled(cancel):
            raise ExportCancelled("Exportação cancelada.")
        # Reconfere o destino imediatamente antes de finalizar.
        replaced = check_destination(conn, destination, fmt, overwrite)
        if replaced:
            os.replace(temp, destination)
        else:
            _finalize_without_overwrite(temp, destination)
    except BaseException as exc:
        _remove_quietly(temp)
        category = getattr(exc, "code", type(exc).__name__)
        logger.warning(
            "Exportação não concluída: tipo=%s id=%s formato=%s categoria=%s registros=%d",
            report.kind,
            report.source_id,
            fmt,
            category,
            rows,
        )
        if isinstance(exc, ExportError):
            raise
        if isinstance(exc, OSError):
            raise ExportError(
                "Não foi possível gravar o relatório no destino escolhido "
                f"({exc.strerror or type(exc).__name__}). Nenhum arquivo foi alterado."
            ) from exc
        raise
    elapsed = round(time.monotonic() - started, 3)
    logger.info(
        "Relatório exportado: tipo=%s id=%s formato=%s conjunto=%s registros=%d bytes=%d "
        "substituiu=%s tempo=%.2fs",
        report.kind,
        report.source_id,
        fmt,
        dataset_key or "todos",
        rows,
        size,
        replaced,
        elapsed,
    )
    return ExportResult(str(destination), fmt, rows, size, elapsed, replaced)


def _cancelled(cancel: Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def _tick(rows: int, total: int, cancel: Event | None, progress: Progress | None) -> None:
    if rows % PROGRESS_EVERY == 0:
        if _cancelled(cancel):
            raise ExportCancelled("Exportação cancelada.")
        if progress is not None:
            progress(rows, total)


def _write_csv(raw, conn, dataset, total, cancel, progress) -> int:
    text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="", write_through=False)
    writer = csv.writer(text, delimiter=CSV_DELIMITER, lineterminator="\r\n")
    writer.writerow([c.header for c in dataset.columns])
    kinds = [c.kind for c in dataset.columns]
    rows = 0
    for row in iterate_rows(conn, dataset):
        writer.writerow([csv_cell(v, k) for v, k in zip(row, kinds)])
        rows += 1
        _tick(rows, total, cancel, progress)
    text.flush()
    text.detach()
    if progress is not None:
        progress(rows, total)
    return rows


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_json(raw, conn, report, datasets, total, cancel, progress) -> int:
    text = io.TextIOWrapper(raw, encoding="utf-8", newline="\n", write_through=False)
    header = {
        "formato": "sysvar-organizer-relatorio",
        "versao_formato": FORMAT_VERSION,
        "aplicativo": {"nome": APP_NAME, "versao": __version__},
        "relatorio": {
            "tipo": report.kind,
            "tipo_descricao": report.kind_label,
            "identificador": report.source_id,
            "titulo": report.title,
            "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "escopo": report.scope,
            "avisos": list(report.notices),
        },
        "resumo": [
            {"campo": i.key, "rotulo": i.label, "valor": json_value(i.value, i.kind)}
            for i in report.summary
        ],
    }
    body = json.dumps(header, ensure_ascii=False, indent=2)
    text.write(body[:-2] + ',\n  "conjuntos": [')
    rows = 0
    for index, dataset in enumerate(datasets):
        meta = {
            "chave": dataset.key,
            "titulo": dataset.title,
            "colunas": [{"chave": c.key, "rotulo": c.header, "tipo": c.kind} for c in dataset.columns],
        }
        text.write(("," if index else "") + "\n    {")
        text.write(", ".join(f"{_dumps(k)}: {_dumps(v)}" for k, v in meta.items()))
        text.write(', "registros": [')
        keys = [c.key for c in dataset.columns]
        kinds = [c.kind for c in dataset.columns]
        count = 0
        for row in iterate_rows(conn, dataset):
            record = {k: json_value(v, t) for k, v, t in zip(keys, row, kinds)}
            text.write(("," if count else "") + "\n      " + _dumps(record))
            count += 1
            rows += 1
            _tick(rows, total, cancel, progress)
        text.write(("\n    " if count else "") + f'], "total_registros": {count}}}')
    text.write("\n  ]\n}\n")
    text.flush()
    text.detach()
    if progress is not None:
        progress(rows, total)
    return rows


def _finalize_without_overwrite(temp: Path, destination: Path) -> None:
    """Torna o temporário definitivo sem jamais substituir um arquivo existente."""
    try:
        os.link(temp, destination)
    except FileExistsError as exc:
        raise DestinationExists("Já existe um arquivo com este nome no destino.") from exc
    except OSError:
        # Sistemas sem hard link (FAT/exFAT, alguns compartilhamentos).
        if sys.platform == "win32":
            try:
                os.rename(temp, destination)  # no Windows, falha se o destino existir
            except FileExistsError as exc:
                raise DestinationExists("Já existe um arquivo com este nome no destino.") from exc
            return
        if os.path.lexists(destination):
            raise DestinationExists("Já existe um arquivo com este nome no destino.") from None
        os.rename(temp, destination)
        return
    os.unlink(temp)


def _remove_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Não foi possível remover o temporário do relatório")
