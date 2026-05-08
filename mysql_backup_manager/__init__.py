"""Public package API for mysql-backup-manager.

Import from this module in application code when you want the stable, supported
surface of the library. The package exposes configuration models, service
classes, result models, helper functions, and the high-level
``MySQLBackupManager`` facade while installing only a ``NullHandler`` so
applications remain in control of logging.
"""

from __future__ import annotations

import logging as _stdlib_logging

_stdlib_logging.getLogger(__name__).addHandler(_stdlib_logging.NullHandler())

from mysql_backup_manager.backup import BackupService, MySQLBackupManager
from mysql_backup_manager.config import (
    DumpConfig,
    MySQLConnectionConfig,
    RestoreConfig,
    RetentionConfig,
    ScheduleConfig,
)
from mysql_backup_manager.models import BackupResult, RestoreResult, RetentionResult
from mysql_backup_manager.restore import RestoreService
from mysql_backup_manager.retention import RetentionService
from mysql_backup_manager.scheduler import SchedulerService
from mysql_backup_manager import helper
from mysql_backup_manager.helper import backup, restore, scheduled_backup

__all__ = [
    "BackupResult",
    "BackupService",
    "DumpConfig",
    "MySQLBackupManager",
    "MySQLConnectionConfig",
    "RestoreConfig",
    "RestoreResult",
    "RestoreService",
    "RetentionConfig",
    "RetentionResult",
    "RetentionService",
    "ScheduleConfig",
    "SchedulerService",
    "backup",
    "helper",
    "restore",
    "scheduled_backup",
]
