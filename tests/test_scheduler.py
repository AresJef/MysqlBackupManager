from __future__ import annotations

import asyncio

from mysql_backup_manager.config import ScheduleConfig
from mysql_backup_manager.scheduler import SchedulerService


class _Retention:
    enabled = True


class _Manager:
    def __init__(self) -> None:
        self.retention = _Retention()
        self.backup_calls = 0
        self.cleanup_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def backup_all(self):
        self.backup_calls += 1
        self.started.set()
        await self.release.wait()
        return []

    async def cleanup_retention(self):
        self.cleanup_calls += 1


async def test_scheduler_skips_overlapping_run() -> None:
    manager = _Manager()
    scheduler = SchedulerService(manager, ScheduleConfig(enabled=True, interval_seconds=60))

    first = asyncio.create_task(scheduler.run_once())
    await manager.started.wait()
    skipped = await scheduler.run_once()
    manager.release.set()
    completed = await first

    assert skipped is False
    assert completed is True
    assert manager.backup_calls == 1
    assert manager.cleanup_calls == 1



class _FailingManager:
    retention = _Retention()

    async def backup_all(self):
        raise RuntimeError("backup exploded")

    async def cleanup_retention(self):
        raise AssertionError("cleanup should not run")


async def test_scheduler_returns_false_when_run_fails() -> None:
    scheduler = SchedulerService(_FailingManager(), ScheduleConfig(enabled=True, interval_seconds=60))

    result = await scheduler.run_once()

    assert result is False
