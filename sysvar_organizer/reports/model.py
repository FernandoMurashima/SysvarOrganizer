"""Estrutura de um relatório: metadados, resumo, avisos e conjuntos de dados.

Cada conjunto de dados é uma consulta SQL ordenada, lida em lotes com
``fetchmany``: a exportação percorre milhões de linhas sem carregá-las em
memória, e a tela consulta apenas a página exibida.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from sysvar_organizer.reports.values import TEXT

FORMAT_VERSION = 1
FETCH_BATCH = 1000


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    kind: str = TEXT


@dataclass(frozen=True)
class SummaryItem:
    key: str
    label: str
    value: object
    kind: str = TEXT


@dataclass(frozen=True)
class Dataset:
    """Consulta ordenada (sem LIMIT) cujas colunas seguem ``columns``.

    ``mapper`` transforma a linha bruta do SQLite na tupla final (rótulos,
    caminhos completos etc.); ``count_sql`` evita junções desnecessárias na
    contagem.
    """

    key: str
    title: str
    columns: tuple[Column, ...]
    sql: str
    params: tuple = ()
    mapper: Callable[[sqlite3.Row | tuple], Sequence] | None = None
    count_sql: str | None = None
    count_params: tuple | None = None

    def map(self, row) -> tuple:
        return tuple(self.mapper(row)) if self.mapper else tuple(row)


@dataclass(frozen=True)
class Report:
    kind: str
    kind_label: str
    source_id: int | None
    title: str
    scope: str
    summary: tuple[SummaryItem, ...]
    notices: tuple[str, ...] = ()
    datasets: tuple[Dataset, ...] = field(default_factory=tuple)

    def dataset(self, key: str) -> Dataset:
        for dataset in self.datasets:
            if dataset.key == key:
                return dataset
        raise KeyError(key)


def count_rows(conn: sqlite3.Connection, dataset: Dataset) -> int:
    if dataset.count_sql:
        params = dataset.count_params if dataset.count_params is not None else dataset.params
        return int(conn.execute(dataset.count_sql, params).fetchone()[0])
    return int(
        conn.execute(f"SELECT COUNT(*) FROM ({dataset.sql})", dataset.params).fetchone()[0]
    )


def page_rows(
    conn: sqlite3.Connection, dataset: Dataset, offset: int, limit: int
) -> list[tuple]:
    rows = conn.execute(f"{dataset.sql} LIMIT ? OFFSET ?", (*dataset.params, limit, offset))
    return [dataset.map(row) for row in rows.fetchall()]


def iterate_rows(conn: sqlite3.Connection, dataset: Dataset) -> Iterator[tuple]:
    cursor = conn.execute(dataset.sql, dataset.params)
    try:
        while True:
            batch = cursor.fetchmany(FETCH_BATCH)
            if not batch:
                return
            for row in batch:
                yield dataset.map(row)
    finally:
        cursor.close()
