"""Tipos de dados da busca de duplicados exatos."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DuplicateProgress:
    candidates: int
    processed: int
    read_count: int
    reused: int
    skipped: int
    bytes_read: int
    current_path: str
    elapsed_seconds: float


@dataclass(frozen=True)
class DuplicateResult:
    search_id: int | None
    status: str
    candidates: int
    processed: int
    read_count: int
    reused: int
    skipped: int
    bytes_read: int
    groups: int | None
    group_files: int | None
    logical_bytes: int | None
    excess_copies: int | None
    recoverable_bytes: int | None
    hardlink_aliases: int | None
    elapsed_seconds: float
    error_message: str | None = None


# Comparação de pastas (Bloco 04) ------------------------------------------


@dataclass(frozen=True)
class ComparisonSet:
    """Conjunto comparado: uma pasta indexada em uma análise concluída."""

    analysis_id: int
    root: str


@dataclass(frozen=True)
class ComparisonProgress:
    total: int
    processed: int
    read_count: int
    reused: int
    bytes_read: int
    current_path: str
    elapsed_seconds: float


@dataclass(frozen=True)
class ComparisonOutcome:
    comparison_id: int | None
    status: str
    files_a: int
    files_b: int
    processed: int
    read_count: int
    reused: int
    bytes_read: int
    elapsed_seconds: float
    error_message: str | None = None
