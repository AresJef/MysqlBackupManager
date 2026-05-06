"""Result models returned by public APIs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class BackupResult(BaseModel):
    """Result from one database backup attempt."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    database: str
    success: bool
    output_file: Path | None
    compressed_file: Path | None
    checksum_file: Path | None
    checksum: str | None
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float
    file_size_bytes: int | None
    command: list[str]
    stderr: str | None
    error: str | None


class RestoreResult(BaseModel):
    """Result from a restore attempt."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool
    input_file: Path
    database: str | None
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float
    command: list[str]
    stderr: str | None
    error: str | None


class RetentionResult(BaseModel):
    """Result from retention cleanup."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool
    deleted_files: list[Path]
    kept_files: list[Path]
    error: str | None

