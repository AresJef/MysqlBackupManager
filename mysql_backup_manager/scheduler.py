"""Scheduled backup execution for long-running applications.

The scheduler repeatedly calls ``MySQLBackupManager.backup_all`` using either a
fixed interval or a cron expression. It is designed for embedding in an asyncio
process, not for daemon management by itself.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from mysql_backup_manager.config import ScheduleConfig
from mysql_backup_manager.exceptions import SchedulerError
from mysql_backup_manager.logging import get_logger


class SchedulerService:
    """Run manager backups on an interval or cron schedule.

    :param manager: ``MySQLBackupManager`` or compatible object exposing ``backup_all``, ``cleanup_retention``, and ``retention``.
    :param config: ``ScheduleConfig`` controlling interval, cron, timezone, and whether to run immediately.
    :param logger: Optional logger for run, skip, and failure messages.
    :return: A ``SchedulerService`` instance.

    ## Example:
    ```python
    # scheduler = SchedulerService(manager, ScheduleConfig(enabled=True, interval_seconds=3600))
    # await scheduler.run_forever()
    ```
    """

    def __init__(
        self,
        manager,
        config: ScheduleConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a scheduler for ``manager`` using ``config``.

        :param manager: Backup manager or compatible test double.
        :param config: Validated schedule configuration.
        :param logger: Optional logger. When omitted, the package logger is used.
        :return: None. The constructor creates the non-overlap lock but does not start a run.
        """

        self.manager = manager
        self.config = config
        self.logger = logger or get_logger(__name__)
        self._lock = asyncio.Lock()
        self.last_results = None
        self.last_retention_result = None
        self.last_error: Exception | None = None

    async def run_once(self) -> bool:
        """Run one scheduled backup cycle and return whether it succeeded.

        :return: ``True`` when a run started, every backup result was successful, and retention cleanup either succeeded or was disabled. ``False`` when another run was active, a backup result failed, retention cleanup failed, or the cycle raised unexpectedly.

        ## Example:
        ```python
        # succeeded = await scheduler.run_once()
        ```
        """

        if self._lock.locked():
            self.logger.warning(
                "Scheduled backup skipped because previous run is still active"
            )
            return False
        async with self._lock:
            self.last_results = None
            self.last_retention_result = None
            self.last_error = None
            try:
                results = await self.manager.backup_all()
                self.last_results = results
                failed_results = [result for result in results if not result.success]
                if failed_results:
                    for result in failed_results:
                        stderr = result.stderr.strip() if result.stderr else None
                        self.logger.error(
                            "Scheduled backup failed for database `%s`: %s%s",
                            result.database,
                            result.error or "backup returned success=False",
                            f"; stderr: {stderr}" if stderr else "",
                        )
                    return False

                if self.manager.retention.enabled:
                    retention_result = await self.manager.cleanup_retention()
                    self.last_retention_result = retention_result
                    if not retention_result.success:
                        self.logger.error(
                            "Scheduled retention cleanup failed: %s",
                            retention_result.error,
                        )
                        return False
                    self.logger.info(
                        "Scheduled retention cleanup completed: deleted %d file(s), kept %d file(s)",
                        len(retention_result.deleted_files),
                        len(retention_result.kept_files),
                    )

                self.logger.info(
                    "Scheduled backup completed successfully for %d database(s)",
                    len(results),
                )
                return True
            except Exception as exc:
                self.last_error = exc
                self.logger.exception("Scheduled backup run failed")
                return False

    def _last_failure_message(self) -> str:
        """Return a concise message for the latest failed scheduled run.

        :return: Human-readable failure detail derived from the latest backup results, retention result, or unhandled exception captured by ``run_once``.
        """

        if self.last_results is not None:
            failed_results = [
                result for result in self.last_results if not result.success
            ]
            if failed_results:
                detail_parts = []
                for result in failed_results:
                    detail = f"`{result.database}`: {result.error or 'backup returned success=False'}"
                    stderr = result.stderr.strip() if result.stderr else None
                    if stderr:
                        detail = f"{detail}; stderr: {stderr}"
                    detail_parts.append(detail)
                return f"Scheduled backup failed: {'; '.join(detail_parts)}"
        if (
            self.last_retention_result is not None
            and not self.last_retention_result.success
        ):
            return f"Scheduled retention cleanup failed: {self.last_retention_result.error}"
        if self.last_error is not None:
            return f"Scheduled backup run failed: {self.last_error}"
        return "Scheduled backup failed"

    def _next_sleep_seconds(self) -> float:
        """Return seconds to wait before the next scheduled backup.

        :return: Number of seconds until the next interval or cron occurrence.
        :raises ValueError: If neither ``cron`` nor ``interval_seconds`` is configured.
        """

        if self.config.interval_seconds is not None:
            return float(self.config.interval_seconds)
        if self.config.cron is None:
            raise ValueError("cron or interval_seconds must be configured")
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        next_run = croniter(self.config.cron, now).get_next(datetime)
        return max((next_run - now).total_seconds(), 0.0)

    async def run_forever(self, *, stop_on_failure: bool = False) -> None:
        """Run scheduled backups until the task is cancelled.

        :param stop_on_failure: When true, raise ``SchedulerError`` after any scheduled cycle returns ``False``. When false, log the failure and continue with the next scheduled run.
        :return: ``None``. For enabled schedules this method normally runs forever until the surrounding asyncio task is cancelled. Disabled schedules log once and return.
        :raises SchedulerError: If ``stop_on_failure`` is true and a scheduled cycle fails or is skipped.

        ## Example:
        ```python
        # async def main():
        #     scheduler = SchedulerService(manager, ScheduleConfig(enabled=True, interval_seconds=3600))
        #     await scheduler.run_forever(stop_on_failure=True)
        ```
        """

        if not self.config.enabled:
            self.logger.info("Scheduler is disabled")
            return
        if self.config.run_immediately:
            succeeded = await self.run_once()
            if stop_on_failure and not succeeded:
                raise SchedulerError(self._last_failure_message())
        while True:
            await asyncio.sleep(self._next_sleep_seconds())
            succeeded = await self.run_once()
            if stop_on_failure and not succeeded:
                raise SchedulerError(self._last_failure_message())
