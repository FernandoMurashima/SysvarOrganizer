"""Tipos de dados da busca de fotografias semelhantes."""

from __future__ import annotations

from dataclasses import dataclass

PHASE_SIGNATURES = "signatures"
PHASE_GROUPING = "grouping"
PHASE_EXACT = "exact"


@dataclass(frozen=True)
class VisualProgress:
    phase: str
    candidates: int
    processed: int
    computed: int
    reused: int
    skipped: int
    bytes_read: int
    current_path: str
    elapsed_seconds: float


@dataclass(frozen=True)
class VisualOutcome:
    search_id: int | None
    status: str
    candidates: int
    processed: int
    computed: int
    reused: int
    skipped: int
    groups: int | None
    grouped_images: int | None
    elapsed_seconds: float
    error_message: str | None = None
