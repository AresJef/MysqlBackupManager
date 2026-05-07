"""Pydantic result models returned by backup, restore, and retention APIs.

Services prefer returning structured models over raising for normal operational
failures. This lets scripts and applications inspect ``success``, stderr, paths,
timings, and command metadata in a consistent way.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class BackupResult(BaseModel):
    """Result from one database backup attempt.

    :param database: Database name that was requested for backup.
    :param success: Whether dump execution and post-processing completed successfully.
    :param output_file: Intended uncompressed ``.sql`` path, even when compression later removed that file.
    :param compressed_file: Final ``.sql.gz`` path when compression was enabled.
    :param checksum_file: Checksum sidecar path when checksum generation succeeded.
    :param checksum: Hex digest written to ``checksum_file``.
    :param started_at: Timezone-aware UTC start timestamp.
    :param finished_at: Timezone-aware UTC finish timestamp.
    :param elapsed_seconds: Runtime in seconds.
    :param file_size_bytes: Size of the final backup artifact when it exists.
    :param command: Sanitized ``mysqldump`` command arguments. Passwords are not present.
    :param stderr: Captured stderr from ``mysqldump`` when available.
    :param error: Human-readable error message when ``success`` is false.
    :return: A Pydantic model instance returned by backup APIs.

    ## Example:
    ```python
    # result = manager.backup_database_sync("app")
    # if result.success: print(result.compressed_file or result.output_file)
    ```
    """

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

    def info(self) -> str:
        """Return a human-readable multi-line summary of the backup result.

        :return: A formatted string containing success status, files, checksum, timings, sanitized command, stderr, and error fields.
        """
        return (
            f"Success: {self.success}\n"
            f"Database: {self.database}\n"
            f"Output File: {self.output_file}\n"
            f"Compressed File: {self.compressed_file}\n"
            f"Checksum File: {self.checksum_file}\n"
            f"Checksum: {self.checksum}\n"
            f"Started At: {self.started_at}\n"
            f"Finished At: {self.finished_at}\n"
            f"Elapsed Seconds: {self.elapsed_seconds:.2f}\n"
            f"File Size (bytes): {self.file_size_bytes}\n"
            f"Command: {' '.join(self.command)}\n"
            f"Stderr: {self.stderr}\n"
            f"Error: {self.error}"
        )


class RestoreResult(BaseModel):
    """Result from one restore attempt.

    :param success: Whether the mysql client finished successfully.
    :param input_file: SQL or SQL.GZ file that was streamed into mysql.
    :param database: Target database requested by ``RestoreConfig``, or ``None`` when the dump chose its own database.
    :param started_at: Timezone-aware UTC start timestamp.
    :param finished_at: Timezone-aware UTC finish timestamp.
    :param elapsed_seconds: Runtime in seconds.
    :param command: Sanitized ``mysql`` command arguments. Passwords are not present.
    :param stderr: Captured stderr from ``mysql`` when available.
    :param error: Human-readable error message when ``success`` is false.
    :return: A Pydantic model instance returned by restore APIs.

    ## Example:
    ```python
    # result = manager.restore_sync(config)
    # if not result.success: raise RuntimeError(result.error)
    ```
    """

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

    def info(self) -> str:
        """Return a human-readable multi-line summary of the restore result.

        :return: A formatted string containing success status, input file, target database, timings, sanitized command, stderr, and error fields.
        """
        return (
            f"Success: {self.success}\n"
            f"Database: {self.database}\n"
            f"Input File: {self.input_file}\n"
            f"Started At: {self.started_at}\n"
            f"Finished At: {self.finished_at}\n"
            f"Elapsed Seconds: {self.elapsed_seconds:.2f}\n"
            f"Command: {' '.join(self.command)}\n"
            f"Stderr: {self.stderr}\n"
            f"Error: {self.error}"
        )


class RetentionResult(BaseModel):
    """Result from retention cleanup.

    :param success: Whether cleanup completed without an unhandled filesystem error.
    :param deleted_files: Files removed by the retention policy.
    :param kept_files: Matching files preserved by the retention policy.
    :param error: Human-readable error message when ``success`` is false.
    :return: A Pydantic model instance returned by retention APIs.

    ## Example:
    ```python
    # result = manager.cleanup_retention_sync()
    # print(result.deleted_files)
    ```
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool
    deleted_files: list[Path]
    kept_files: list[Path]
    error: str | None

    def info(self) -> str:
        """Return a human-readable multi-line summary of cleanup activity.

        :return: A formatted string containing success status, deleted files, kept files, and any error message.
        """

        return (
            f"Success: {self.success}\n"
            f"Deleted Files: {', '.join(str(f) for f in self.deleted_files)}\n"
            f"Kept Files: {', '.join(str(f) for f in self.kept_files)}\n"
            f"Error: {self.error}"
        )
