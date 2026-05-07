"""Exception hierarchy for mysql-backup-manager.

The library returns structured result models for expected command failures, but
configuration errors, missing executables, and low-level command execution
problems use these exception types internally. Catch the specific subclasses in
application code when you need to distinguish backup, restore, retention, and
scheduler failures.
"""

from __future__ import annotations


class MySQLBackupError(Exception):
    """Base exception for backup-related failures.

    :param *args: Standard exception message arguments.
    :return: A ``MySQLBackupError`` instance when raised or constructed.
    """


class MySQLRestoreError(Exception):
    """Base exception for restore-related failures.

    :param *args: Standard exception message arguments.
    :return: A ``MySQLRestoreError`` instance when raised or constructed.
    """


class MySQLCommandError(Exception):
    """Raised when a native MySQL client command fails.

    :param message: Human-readable failure message.
    :param returncode: Optional process exit status.
    :param stderr: Optional captured stderr text from the native client.
    :return: A ``MySQLCommandError`` instance carrying command diagnostics.

    ## Example:
    ```python
    error = MySQLCommandError("failed", returncode=1, stderr="ERROR")
    error.returncode
    # 1
    ```
    """

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str | None = None) -> None:
        """Create a command error with optional process diagnostics.

        :param message: Human-readable failure message.
        :param returncode: Optional subprocess return code.
        :param stderr: Optional stderr captured from the subprocess.
        :return: None.
        """

        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class MySQLDumpNotFoundError(MySQLBackupError):
    """Raised when the configured ``mysqldump`` executable cannot be found.

    :param *args: Standard exception message arguments.
    :return: A ``MySQLDumpNotFoundError`` instance when raised or constructed.
    """


class MySQLClientNotFoundError(MySQLRestoreError):
    """Raised when the configured ``mysql`` executable cannot be found.

    :param *args: Standard exception message arguments.
    :return: A ``MySQLClientNotFoundError`` instance when raised or constructed.
    """


class BackupConfigError(MySQLBackupError):
    """Raised when ``DumpConfig`` receives invalid or unsafe values.

    :param *args: Standard exception message arguments.
    :return: A ``BackupConfigError`` instance when raised or constructed.
    """


class RestoreConfigError(MySQLRestoreError):
    """Raised when ``RestoreConfig`` receives invalid or unsafe values.

    :param *args: Standard exception message arguments.
    :return: A ``RestoreConfigError`` instance when raised or constructed.
    """


class RetentionError(Exception):
    """Raised for retention cleanup failures.

    :param *args: Standard exception message arguments.
    :return: A ``RetentionError`` instance when raised or constructed.
    """


class SchedulerError(Exception):
    """Raised for scheduler setup or execution failures.

    :param *args: Standard exception message arguments.
    :return: A ``SchedulerError`` instance when raised or constructed.
    """

