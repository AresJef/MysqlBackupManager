"""Helper functions for replica-oriented MySQL backup and restore.

These helpers are intentionally not imported by the package root. Import them
from ``mysql_backup_manager.helper`` or treat this module as copyable application
code that shows how to compose the library safely for one-off backups, one-off
restores, and long-running scheduled backup jobs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mysql_backup_manager import (
    DumpConfig,
    MySQLBackupManager,
    MySQLConnectionConfig,
    RestoreConfig,
    RetentionConfig,
    ScheduleConfig,
    SchedulerService,
)
from mysql_backup_manager.checksum import compute_checksum

logger = logging.getLogger(__name__)

REPLICA_BOOTSTRAP_OPTIONS = [
    "--databases",
    "--quick",
    "--hex-blob",
]

RESTORE_OPTIONS = [
    "--binary-mode",
]


def verify_checksum(backup_file: Path | str) -> None:
    """Verify ``backup_file`` against its adjacent ``.sha256`` sidecar.

    :param backup_file: Path or string path to the ``.sql`` or ``.sql.gz`` backup artifact. ``~`` is expanded before lookup.
    :return: None when the checksum sidecar exists and the computed digest matches.
    :raises RuntimeError: If the backup file is missing, the sidecar is missing, or the digest does not match.

    ## Example:
    ```python
    verify_checksum(Path("~/Downloads/backups/app_20260507.sql.gz"))
    ```
    """

    backup_file = Path(backup_file).expanduser()
    checksum_file = backup_file.with_name(f"{backup_file.name}.sha256")

    if not backup_file.exists() or not backup_file.is_file():
        raise RuntimeError(f"Backup file not found: {backup_file}")
    if not checksum_file.exists() or not checksum_file.is_file():
        raise RuntimeError(f"Checksum file not found: {checksum_file}")

    checksum_parts = checksum_file.read_text(encoding="utf-8").split()
    if not checksum_parts:
        raise RuntimeError(f"Checksum file is empty or invalid: {checksum_file}")
    expected = checksum_parts[0]
    actual = compute_checksum(backup_file, "sha256")

    if actual != expected:
        raise RuntimeError(
            f"Checksum mismatch for {backup_file}. Expected {expected}, got {actual}"
        )


def _build_replica_backup_manager(
    *,
    backup_dir: Path | str,
    database: str,
    host: str,
    port: int,
    user: str,
    password: str,
    command_timeout: int,
    retention: RetentionConfig | None = None,
) -> MySQLBackupManager:
    """Build a manager configured for large replica bootstrap backups.

    :param backup_dir: Path or string path to the directory where source backups should be written.
    :param database: Database name to include in the replica bootstrap dump.
    :param host: Source MySQL host.
    :param port: Source MySQL port.
    :param user: Source MySQL user.
    :param password: Source MySQL password.
    :param command_timeout: Maximum dump runtime in seconds.
    :param retention: Optional retention policy for scheduled backup use.
    :return: ``MySQLBackupManager`` configured with gzip compression, SHA-256 checksum generation, ``--databases``, ``--quick``, ``--hex-blob``, and ``--set-gtid-purged=ON``.
    """

    backup_dir = Path(backup_dir).expanduser()

    return MySQLBackupManager(
        connection=MySQLConnectionConfig(
            host=host,
            port=port,
            user=user,
            password=password,
        ),
        dump=DumpConfig(
            databases=[database],
            output_dir=backup_dir,
            compress=True,
            generate_checksum=True,
            checksum_algorithm="sha256",
            set_gtid_purged="ON",
            single_transaction=True,
            lock_tables=False,
            routines=True,
            triggers=True,
            events=True,
            add_drop_table=True,
            extra_options=REPLICA_BOOTSTRAP_OPTIONS,
            command_timeout=command_timeout,
            overwrite=False,
        ),
        retention=retention,
    )


def scheduled_backup(
    *,
    backup_dir: Path | str,
    database: str,
    host: str,
    port: int,
    user: str,
    password: str,
    command_timeout: int = 3600,
    interval_seconds: int | None = None,
    cron: str | None = None,
    timezone: str = "UTC",
    run_immediately: bool = True,
    stop_on_failure: bool = True,
    keep_last: int | None = 10,
    keep_days: int | None = None,
) -> None:
    """Run replica-bootstrap backups forever on an interval or cron schedule.

    :param backup_dir: Path or string path where scheduled backups and checksum sidecars are written.
    :param database: Database to back up on each run.
    :param host: Source MySQL host.
    :param port: Source MySQL port.
    :param user: Source MySQL user.
    :param password: Source MySQL password.
    :param command_timeout: Maximum runtime in seconds for each dump command.
    :param interval_seconds: Fixed interval between runs. Mutually exclusive with ``cron``.
    :param cron: Cron expression such as ``"0 3 * * *"``. Mutually exclusive with ``interval_seconds``.
    :param timezone: IANA timezone used for cron evaluation.
    :param run_immediately: Run one backup before waiting for the first scheduled time.
    :param stop_on_failure: Raise and stop the loop when a scheduled backup cycle fails. Keep this true for foreground scripts so bad credentials or bad MySQL options fail fast; set it false for daemon-style jobs that should keep retrying on the next interval.
    :param keep_last: Delete backup artifacts beyond this newest-file count. Set to ``None`` to disable count-based deletion.
    :param keep_days: Delete backup artifacts older than this many days. Set to ``None`` to disable age-based deletion. When this is set together with ``keep_last``, a backup is deleted if it exceeds either limit.
    :return: None. The function blocks by running ``SchedulerService.run_forever`` until interrupted or cancelled.

    ## Example:
    ```python
    scheduled_backup(
        backup_dir=Path("~/Downloads/backups"),
        database="app",
        host="localhost",
        port=3306,
        user="root",
        password="secret",
        interval_seconds=3600,
    )
    ```
    """

    manager = _build_replica_backup_manager(
        backup_dir=backup_dir,
        database=database,
        host=host,
        port=port,
        user=user,
        password=password,
        command_timeout=command_timeout,
        retention=RetentionConfig(
            enabled=True,
            keep_last=keep_last,
            keep_days=keep_days,
            match_pattern=f"{database}_*.sql.gz",
        ),
    )

    scheduler = SchedulerService(
        manager=manager,
        config=ScheduleConfig(
            enabled=True,
            interval_seconds=interval_seconds,
            cron=cron,
            timezone=timezone,
            run_immediately=run_immediately,
        ),
    )

    asyncio.run(scheduler.run_forever(stop_on_failure=stop_on_failure))


def backup(
    *,
    backup_dir: Path | str,
    database: str,
    host: str,
    port: int,
    user: str,
    password: str,
    command_timeout: int = 3600,
) -> Path:
    """Create one compressed, checksummed, GTID-preserving backup.

    :param backup_dir: Path or string path where the backup artifact and checksum sidecar are written.
    :param database: Source database to dump.
    :param host: Source MySQL host.
    :param port: Source MySQL port.
    :param user: Source MySQL user.
    :param password: Source MySQL password.
    :param command_timeout: Maximum dump runtime in seconds.
    :return: Path to the final backup artifact, normally ``.sql.gz``.
    :raises RuntimeError: If the backup fails or no output file is returned.

    ## Example:
    ```python
    backup_file = backup(
        backup_dir=Path("~/Downloads/replica-bootstrap"),
        database="app",
        host="source.example.com",
        port=3306,
        user="backup_user",
        password="secret",
    )
    ```
    """

    manager = _build_replica_backup_manager(
        backup_dir=backup_dir,
        database=database,
        host=host,
        port=port,
        user=user,
        password=password,
        command_timeout=command_timeout,
    )

    result = manager.backup_database_sync(database)

    if not result.success:
        raise RuntimeError(f"Backup failed: {result.error}\nSTDERR: {result.stderr}")

    backup_file = result.compressed_file or result.output_file
    if backup_file is None:
        raise RuntimeError("Backup succeeded but no output file was returned")

    return backup_file


def restore(
    *,
    backup_file: Path | str,
    database: str,
    host: str,
    port: int,
    user: str,
    password: str,
    command_timeout: int = 3600,
) -> None:
    """Restore a GTID-preserving backup for replica bootstrap.

    :param backup_file: Path or string path to the transferred ``.sql`` or ``.sql.gz`` backup file. The backup directory is derived from this path.
    :param database: Logical database name associated with the backup. The dump itself chooses the target database because restore uses ``database=None``.
    :param host: Replica MySQL host.
    :param port: Replica MySQL port.
    :param user: Replica MySQL user.
    :param password: Replica MySQL password.
    :param command_timeout: Maximum restore runtime in seconds.
    :return: None when restore completes successfully.
    :raises RuntimeError: If checksum verification fails or restore returns ``success=False``.

    ## Example:
    ```python
    restore(
        backup_file=backup_file,
        database="app",
        host="replica.example.com",
        port=3306,
        user="restore_user",
        password="secret",
    )
    ```
    """

    backup_file = Path(backup_file).expanduser()
    backup_dir = backup_file.parent

    verify_checksum(backup_file)

    manager = MySQLBackupManager(
        connection=MySQLConnectionConfig(
            host=host,
            port=port,
            user=user,
            password=password,
        ),
        dump=DumpConfig(
            databases=[database],
            output_dir=backup_dir,
        ),
    )

    result = manager.restore_sync(
        RestoreConfig(
            database=None,
            input_file=backup_file,
            strip_gtid_purged=False,
            decompress=True,
            force=False,
            extra_options=RESTORE_OPTIONS,
            command_timeout=command_timeout,
        )
    )

    if not result.success:
        raise RuntimeError(f"Restore failed: {result.error}\nSTDERR: {result.stderr}")
