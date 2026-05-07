"""Retention cleanup service for backup directories.

Retention applies conservative deletion rules to files matching a relative glob
inside a configured output directory. It never follows a pattern outside that
directory and records both deleted and kept files in ``RetentionResult``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mysql_backup_manager.utils import utc_now
from mysql_backup_manager.config import RetentionConfig
from mysql_backup_manager.logging import get_logger
from mysql_backup_manager.models import RetentionResult

_CHECKSUM_SUFFIXES = (".sha256", ".md5")


class RetentionService:
    """Apply a retention policy to one backup output directory.

    :param output_dir: Directory whose matching backup files may be cleaned up.
    :param config: ``RetentionConfig`` describing deletion limits and the match pattern.
    :param logger: Optional logger for deletion and failure messages.
    :return: A ``RetentionService`` instance.

    ## Example:
    ```python
    from pathlib import Path
    service = RetentionService(Path("./backups"), RetentionConfig(keep_last=10, keep_days=30))
    isinstance(service.config.enabled, bool)
    # True
    ```
    """

    def __init__(
        self,
        output_dir: Path | str,
        config: RetentionConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a retention service for ``output_dir`` and ``config``.

        :param output_dir: Backup directory to inspect. ``str`` values are converted to ``Path`` and ``~`` is expanded. Only files resolving inside this directory are eligible for deletion.
        :param config: Retention policy to apply.
        :param logger: Optional logger. When omitted, the package logger is used.
        :return: None.
        """

        self.output_dir = Path(output_dir).expanduser()
        self.config = config
        self.logger = logger or get_logger(__name__)

    def _is_inside_output_dir(self, path: Path) -> bool:
        """Return whether ``path`` resolves within ``self.output_dir``.

        :param path: Candidate file path.
        :return: ``True`` when the resolved path is inside the resolved output directory, otherwise ``False``.
        """

        output_dir = self.output_dir.resolve()
        try:
            path.resolve().relative_to(output_dir)
        except ValueError:
            return False
        return True

    def _candidate_files(self) -> list[Path]:
        """Return matching backup files that are safe cleanup candidates.

        :return: List of regular files matching ``RetentionConfig.match_pattern`` that also resolve inside ``output_dir``. Missing output directories return an empty list.
        """

        if not self.output_dir.exists():
            return []

        patterns = [self.config.match_pattern]
        patterns.extend(f"{self.config.match_pattern}{suffix}" for suffix in _CHECKSUM_SUFFIXES)
        candidates: dict[Path, None] = {}
        for pattern in patterns:
            for path in self.output_dir.glob(pattern):
                if path.is_file() and self._is_inside_output_dir(path):
                    candidates[path] = None
        return list(candidates)

    def _is_checksum_sidecar(self, path: Path) -> bool:
        """Return whether ``path`` is a checksum sidecar file.

        :param path: Candidate file path.
        :return: ``True`` for files ending in ``.sha256`` or ``.md5``; otherwise ``False``.
        """

        return path.name.endswith(_CHECKSUM_SUFFIXES)

    def _sidecar_files(self, backup_file: Path) -> list[Path]:
        """Return checksum sidecars that belong to ``backup_file``.

        :param backup_file: Primary backup artifact such as ``.sql`` or ``.sql.gz``.
        :return: Existing checksum sidecar paths inside ``output_dir``.
        """

        sidecars = [backup_file.with_name(f"{backup_file.name}{suffix}") for suffix in _CHECKSUM_SUFFIXES]
        return [path for path in sidecars if path.exists() and path.is_file() and self._is_inside_output_dir(path)]

    def _backup_file_for_sidecar(self, sidecar_file: Path) -> Path | None:
        """Return the primary backup artifact path for a checksum sidecar.

        :param sidecar_file: Candidate checksum sidecar path.
        :return: Primary backup path when ``sidecar_file`` has a supported checksum suffix; otherwise ``None``.
        """

        for suffix in _CHECKSUM_SUFFIXES:
            if sidecar_file.name.endswith(suffix):
                return sidecar_file.with_name(sidecar_file.name[: -len(suffix)])
        return None

    def _delete_file(self, path: Path, deleted_files: list[Path]) -> None:
        """Delete one file and record it in ``deleted_files``.

        :param path: File to delete. It must resolve inside ``output_dir``.
        :param deleted_files: Mutable list that receives deleted paths.
        :return: None.
        """

        if not self._is_inside_output_dir(path) or not path.exists() or not path.is_file():
            return
        path.unlink()
        deleted_files.append(path)
        self.logger.info("Deleted backup due to retention policy: %s", path)

    def cleanup(self) -> RetentionResult:
        """Delete backup files that do not satisfy the retention policy.

        :return: ``RetentionResult`` listing deleted files, kept files, success status, and any error message. ``keep_last`` counts primary backup artifacts, not checksum sidecars. If both ``keep_last`` and ``keep_days`` are configured, a backup is deleted when it violates either rule. Set either option to ``None`` to disable that deletion rule.

        ## Example:
        ```python
        # result = RetentionService(Path("./backups"), RetentionConfig()).cleanup()
        # print(result.info())
        ```
        """

        deleted_files: list[Path] = []
        kept_files: list[Path] = []
        try:
            if not self.config.enabled:
                kept_files = self._candidate_files()
                return RetentionResult(success=True, deleted_files=[], kept_files=kept_files, error=None)

            candidates = self._candidate_files()
            files = sorted(
                (path for path in candidates if not self._is_checksum_sidecar(path)),
                key=lambda path: (path.stat().st_mtime, path.name),
                reverse=True,
            )
            delete_set: set[Path] = set()

            if self.config.keep_last is not None:
                delete_set.update(files[self.config.keep_last :])
            if self.config.keep_days is not None:
                cutoff = utc_now() - timedelta(days=self.config.keep_days)
                delete_set.update(
                    path
                    for path in files
                    if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) < cutoff
                )

            for path in files:
                sidecars = self._sidecar_files(path)
                if not self._is_inside_output_dir(path):
                    kept_files.append(path)
                    kept_files.extend(sidecars)
                    continue
                if path in delete_set:
                    self._delete_file(path, deleted_files)
                    for sidecar in sidecars:
                        self._delete_file(sidecar, deleted_files)
                    continue
                kept_files.append(path)
                kept_files.extend(sidecars)

            sidecars_for_known_files = {
                path.with_name(f"{path.name}{suffix}")
                for path in files
                for suffix in _CHECKSUM_SUFFIXES
            }
            cleanup_limits_enabled = self.config.keep_last is not None or self.config.keep_days is not None
            for sidecar in sorted(path for path in candidates if self._is_checksum_sidecar(path)):
                if sidecar in sidecars_for_known_files:
                    continue
                backup_file = self._backup_file_for_sidecar(sidecar)
                backup_exists = (
                    backup_file is not None
                    and backup_file.exists()
                    and backup_file.is_file()
                    and self._is_inside_output_dir(backup_file)
                )
                if backup_exists:
                    kept_files.append(sidecar)
                    continue
                if cleanup_limits_enabled:
                    self._delete_file(sidecar, deleted_files)
                else:
                    kept_files.append(sidecar)

            result = RetentionResult(success=True, deleted_files=deleted_files, kept_files=kept_files, error=None)
            self.logger.info(
                "Retention cleanup completed in %s: deleted %d file(s), kept %d file(s); keep_last=%s, keep_days=%s, match_pattern=%s",
                self.output_dir,
                len(result.deleted_files),
                len(result.kept_files),
                self.config.keep_last,
                self.config.keep_days,
                self.config.match_pattern,
            )
            return result
        except Exception as exc:
            self.logger.exception("Retention cleanup failed")
            return RetentionResult(success=False, deleted_files=deleted_files, kept_files=kept_files, error=str(exc))

