"""Tipos de dados da varredura."""

from __future__ import annotations

from dataclasses import dataclass

# Estados gravados em analysis_history.status
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"

KIND_FOLDER = "folder"
KIND_DRIVE = "drive"


@dataclass(frozen=True)
class ScanLocation:
    path: str
    kind: str = KIND_FOLDER


@dataclass(frozen=True)
class ScanProgress:
    files: int
    folders: int
    total_bytes: int
    inaccessible: int
    skipped: int
    current_path: str
    elapsed_seconds: float


@dataclass(frozen=True)
class ScanResult:
    analysis_id: int | None
    status: str
    files: int
    folders: int
    total_bytes: int
    inaccessible: int
    skipped: int
    elapsed_seconds: float
    has_warnings: bool
    error_message: str | None = None
