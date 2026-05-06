"""Retention cleanup service."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mysql_backup_manager.backup import utc_now
from mysql_backup_manager.config import RetentionConfig
from mysql_backup_manager.logging import get_logger
from mysql_backup_manager.models import RetentionResult


class RetentionService:
    """Clean up old backup files inside a configured output directory."""

    def __init__(
        self,
        output_dir: Path,
        config: RetentionConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.config = config
        self.logger = logger or get_logger(__name__)

    def _is_inside_output_dir(self, path: Path) -> bool:
        output_dir = self.output_dir.resolve()
        try:
            path.resolve().relative_to(output_dir)
        except ValueError:
            return False
        return True

    def _candidate_files(self) -> list[Path]:
        if not self.output_dir.exists():
            return []
        return [
            path
            for path in self.output_dir.glob(self.config.match_pattern)
            if path.is_file() and self._is_inside_output_dir(path)
        ]

    def cleanup(self) -> RetentionResult:
        """Delete files that do not satisfy retention rules."""

        deleted_files: list[Path] = []
        kept_files: list[Path] = []
        try:
            if not self.config.enabled:
                kept_files = self._candidate_files()
                return RetentionResult(success=True, deleted_files=[], kept_files=kept_files, error=None)

            files = sorted(self._candidate_files(), key=lambda path: path.stat().st_mtime, reverse=True)
            keep_set: set[Path] = set()

            if self.config.keep_last is not None:
                keep_set.update(files[: self.config.keep_last])
            if self.config.keep_days is not None:
                cutoff = utc_now() - timedelta(days=self.config.keep_days)
                keep_set.update(
                    path
                    for path in files
                    if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) >= cutoff
                )

            for path in files:
                if path in keep_set:
                    kept_files.append(path)
                    continue
                if not self._is_inside_output_dir(path):
                    kept_files.append(path)
                    continue
                path.unlink()
                deleted_files.append(path)
                self.logger.info("Deleted backup due to retention policy: %s", path)

            return RetentionResult(success=True, deleted_files=deleted_files, kept_files=kept_files, error=None)
        except Exception as exc:
            self.logger.exception("Retention cleanup failed")
            return RetentionResult(success=False, deleted_files=deleted_files, kept_files=kept_files, error=str(exc))

