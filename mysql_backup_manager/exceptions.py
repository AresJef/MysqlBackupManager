"""Custom exceptions for mysql-backup-manager."""

from __future__ import annotations


class MySQLBackupError(Exception):
    """Base exception for backup failures."""


class MySQLRestoreError(Exception):
    """Base exception for restore failures."""


class MySQLCommandError(Exception):
    """Raised when a MySQL client command exits unsuccessfully."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class MySQLDumpNotFoundError(MySQLBackupError):
    """Raised when mysqldump cannot be executed."""


class MySQLClientNotFoundError(MySQLRestoreError):
    """Raised when mysql cannot be executed."""


class BackupConfigError(MySQLBackupError):
    """Raised when backup configuration is invalid."""


class RestoreConfigError(MySQLRestoreError):
    """Raised when restore configuration is invalid."""


class RetentionError(Exception):
    """Raised when retention cleanup fails."""


class SchedulerError(Exception):
    """Raised when scheduling fails."""

