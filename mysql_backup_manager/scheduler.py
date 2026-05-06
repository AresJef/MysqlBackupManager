"""Scheduled backup execution."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from mysql_backup_manager.config import ScheduleConfig
from mysql_backup_manager.logging import get_logger


class SchedulerService:
    """Run backups on an interval or cron schedule."""

    def __init__(
        self,
        manager,
        config: ScheduleConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.manager = manager
        self.config = config
        self.logger = logger or get_logger(__name__)
        self._lock = asyncio.Lock()

    async def run_once(self) -> bool:
        """Run backup_all and retention cleanup unless another run is active."""

        if self._lock.locked():
            self.logger.warning("Scheduled backup skipped because previous run is still active")
            return False
        async with self._lock:
            try:
                results = await self.manager.backup_all()
                if all(result.success for result in results) and self.manager.retention.enabled:
                    await self.manager.cleanup_retention()
                return True
            except Exception:
                self.logger.exception("Scheduled backup run failed")
                return False

    def _next_sleep_seconds(self) -> float:
        if self.config.interval_seconds is not None:
            return float(self.config.interval_seconds)
        if self.config.cron is None:
            raise ValueError("cron or interval_seconds must be configured")
        tz = ZoneInfo(self.config.timezone)
        now = datetime.now(tz)
        next_run = croniter(self.config.cron, now).get_next(datetime)
        return max((next_run - now).total_seconds(), 0.0)

    async def run_forever(self) -> None:
        """Run scheduled backups forever until cancelled."""

        if not self.config.enabled:
            self.logger.info("Scheduler is disabled")
            return
        if self.config.run_immediately:
            await self.run_once()
        while True:
            await asyncio.sleep(self._next_sleep_seconds())
            await self.run_once()

