"""High-level convenience functions for MySQL backup and restore sessions.

The package's service classes remain the most explicit integration surface, but
these helpers are intended to be the easy synchronous API for application code,
small operational scripts, and scheduled jobs. They expose the options most
callers need while using stable, sensible defaults for lower-level settings,
including the native ``mysqldump`` and ``mysql`` executable names. Use the
configuration models and services directly when custom executable paths are
required.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from mysql_backup_manager.backup import MySQLBackupManager
from mysql_backup_manager.checksum import compute_checksum
from mysql_backup_manager.config import (
    DumpConfig,
    MySQLConnectionConfig,
    RestoreConfig,
    RetentionConfig,
    ScheduleConfig,
)
from mysql_backup_manager.models import BackupResult, RestoreResult
from mysql_backup_manager.restore import RestoreService
from mysql_backup_manager.scheduler import SchedulerService
from mysql_backup_manager.utils import ensure_no_running_loop

logger = logging.getLogger(__name__)

ChecksumAlgorithm = Literal["sha256", "md5"]
GtidPurgedValue = Literal["AUTO", "ON", "OFF"]
DatabaseSelection = str | Sequence[str]
BackupReturn = Path | list[Path] | BackupResult | list[BackupResult]
RestoreReturn = RestoreResult | None

_DEFAULT_CHARACTER_SET = "utf8mb4"
_DEFAULT_CONNECT_TIMEOUT = 10
_DEFAULT_FILENAME_TEMPLATE = "{database}_{timestamp}.sql"
_DEFAULT_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
_DEFAULT_MYSQLDUMP_PATH = "mysqldump"
_DEFAULT_MYSQL_PATH = "mysql"
_DEFAULT_COMPRESSION_FORMAT = "gzip"
_DEFAULT_CHECKSUM_ALGORITHM: ChecksumAlgorithm = "sha256"


def verify_checksum(
    backup_file: Path | str,
    algorithm: ChecksumAlgorithm = _DEFAULT_CHECKSUM_ALGORITHM,
) -> None:
    """Verify ``backup_file`` against its adjacent checksum sidecar.

    :param backup_file: Path or string path to the ``.sql`` or ``.sql.gz`` backup artifact. ``~`` is expanded before lookup.
    :param algorithm: Checksum algorithm whose sidecar should be read. ``"sha256"`` expects ``<backup>.sha256`` and ``"md5"`` expects ``<backup>.md5``.
    :return: None when the checksum sidecar exists and the computed digest matches.
    :raises RuntimeError: If the backup file is missing, the sidecar is missing, the sidecar is invalid, or the digest does not match.

    ## Example:
    ```python
    verify_checksum(Path("~/Downloads/backups/app_20260507.sql.gz"))
    ```
    """

    backup_path = Path(backup_file).expanduser()
    checksum_file = backup_path.with_name(f"{backup_path.name}.{algorithm}")

    if not backup_path.exists() or not backup_path.is_file():
        raise RuntimeError(f"Backup file not found: {backup_path}")
    if not checksum_file.exists() or not checksum_file.is_file():
        raise RuntimeError(f"Checksum file not found: {checksum_file}")

    checksum_parts = checksum_file.read_text(encoding="utf-8").split()
    if not checksum_parts:
        raise RuntimeError(f"Checksum file is empty or invalid: {checksum_file}")
    expected = checksum_parts[0]
    actual = compute_checksum(backup_path, algorithm)

    if actual != expected:
        raise RuntimeError(
            f"Checksum mismatch for {backup_path}. Expected {expected}, got {actual}"
        )


def _normalize_databases(database: DatabaseSelection) -> list[str]:
    """Return a non-empty database list from helper ``database`` input.

    :param database: One database name as a string, or a sequence of database names.
    :return: Non-empty list of database names.
    :raises ValueError: If ``database`` is ``None`` or an empty sequence.
    """

    if database is None:
        raise ValueError("database must be a database name or a sequence of names")
    if isinstance(database, str):
        return [database]
    normalized = list(database)
    if not normalized:
        raise ValueError("database must contain at least one database name")
    return normalized


def _build_connection_config(
    *,
    host: str,
    port: int,
    user: str,
    password: str | None,
    socket: str | None,
) -> MySQLConnectionConfig:
    """Build shared MySQL connection config for helper functions.

    :param host: MySQL server host.
    :param port: MySQL server TCP port.
    :param user: MySQL account name.
    :param password: Optional password passed through ``MYSQL_PWD``.
    :param socket: Optional Unix socket path.
    :return: Validated ``MySQLConnectionConfig`` using helper defaults for character set and connection timeout.
    """

    return MySQLConnectionConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        socket=socket,
        default_character_set=_DEFAULT_CHARACTER_SET,
        connect_timeout=_DEFAULT_CONNECT_TIMEOUT,
    )


def _dump_extra_options(
    *,
    extra_options: Sequence[str] | None,
    include_database_statements: bool,
    quick: bool,
    hex_blob: bool,
) -> list[str]:
    """Return mysqldump extra options with helper-managed flags included.

    :param extra_options: Raw mysqldump options supplied by the caller.
    :param include_database_statements: Add ``--databases`` when true so the dump contains ``CREATE DATABASE`` and ``USE`` statements.
    :param quick: Add ``--quick`` when true and not already present.
    :param hex_blob: Add ``--hex-blob`` when true and not already present.
    :return: List of extra mysqldump options.
    """

    options = [option.strip() for option in (extra_options or [])]
    prefix: list[str] = []
    if include_database_statements and "--databases" not in options and "-B" not in options:
        prefix.append("--databases")
    if quick and "--quick" not in options:
        prefix.append("--quick")
    if hex_blob and "--hex-blob" not in options:
        prefix.append("--hex-blob")
    return prefix + options


def _build_backup_manager(
    *,
    databases: Sequence[str],
    backup_dir: Path | str,
    temp_dir: Path | str | None,
    host: str,
    port: int,
    user: str,
    password: str | None,
    socket: str | None,
    command_timeout: float | None,
    validate_database_exists: bool,
    validate_database_has_objects: bool,
    validate_dump_content: bool,
    cleanup_stale_temp_files: bool,
    stale_temp_file_age_seconds: float | None,
    single_transaction: bool,
    routines: bool,
    triggers: bool,
    events: bool,
    add_drop_database: bool,
    add_drop_table: bool,
    create_options: bool,
    lock_tables: bool,
    flush_logs: bool,
    master_data: int | None,
    set_gtid_purged: GtidPurgedValue | None,
    where: str | None,
    include_database_statements: bool,
    quick: bool,
    hex_blob: bool,
    ignore_tables: Sequence[str] | None,
    extra_options: Sequence[str] | None,
    compress: bool,
    generate_checksum: bool,
    overwrite: bool,
    retention: RetentionConfig | None = None,
    logger: logging.Logger | None = None,
) -> MySQLBackupManager:
    """Build a backup manager for helper workflows.

    The helper intentionally uses the default ``mysqldump`` and ``mysql``
    executable names. Use ``DumpConfig`` and ``MySQLBackupManager`` directly
    when an application needs custom executable paths.

    :param databases: Database names that may be backed up by the manager.
    :param backup_dir: Directory where backup artifacts are written.
    :param temp_dir: Optional directory for active hidden ``.part`` staging files. Defaults to ``~/.MysqlBackupManager`` or ``MYSQL_BACKUP_MANAGER_TEMP_DIR`` when omitted.
    :param host: MySQL server host.
    :param port: MySQL server TCP port.
    :param user: MySQL account name.
    :param password: Optional password passed through ``MYSQL_PWD``.
    :param socket: Optional Unix socket path.
    :param command_timeout: Optional maximum runtime in seconds for each dump command.
    :param validate_database_exists: Verify database visibility before dumping.
    :param validate_database_has_objects: Verify at least one table/view is visible before dumping.
    :param validate_dump_content: Verify dump output contains schema or data markers before finalizing.
    :param cleanup_stale_temp_files: Best-effort cleanup of stale library-created ``.part`` files before dumping.
    :param stale_temp_file_age_seconds: Minimum temp-file age before stale cleanup deletes it.
    :param single_transaction: Add ``--single-transaction`` when true.
    :param routines: Include stored routines.
    :param triggers: Include triggers.
    :param events: Include events.
    :param add_drop_database: Add ``--add-drop-database``.
    :param add_drop_table: Add ``--add-drop-table``.
    :param create_options: Preserve MySQL-specific create options.
    :param lock_tables: Add ``--lock-tables`` when true; otherwise ``--skip-lock-tables``.
    :param flush_logs: Add ``--flush-logs``.
    :param master_data: Optional ``--master-data`` value.
    :param set_gtid_purged: Optional ``--set-gtid-purged`` value: ``"AUTO"``, ``"ON"``, or ``"OFF"``.
    :param where: Optional ``--where`` condition.
    :param include_database_statements: Add ``--databases`` so each dump includes ``CREATE DATABASE`` and ``USE`` statements and can select its own target database during restore.
    :param quick: Add ``--quick`` to mysqldump extra options when true.
    :param hex_blob: Add ``--hex-blob`` to mysqldump extra options when true.
    :param ignore_tables: Optional ``db.table`` entries to skip.
    :param extra_options: Additional raw mysqldump options appended before the database name.
    :param compress: Produce ``.sql.gz`` output when true.
    :param generate_checksum: Write SHA-256 checksum sidecar when true.
    :param overwrite: Replace existing final backup files when true.
    :param retention: Optional retention policy used by scheduled backups.
    :param logger: Optional logger shared by manager services.
    :return: Configured ``MySQLBackupManager``.
    """

    connection = _build_connection_config(
        host=host,
        port=port,
        user=user,
        password=password,
        socket=socket,
    )
    dump = DumpConfig(
        databases=list(databases),
        output_dir=Path(backup_dir).expanduser(),
        temp_dir=Path(temp_dir).expanduser() if temp_dir is not None else None,
        filename_template=_DEFAULT_FILENAME_TEMPLATE,
        timestamp_format=_DEFAULT_TIMESTAMP_FORMAT,
        mysqldump_path=_DEFAULT_MYSQLDUMP_PATH,
        mysql_path=_DEFAULT_MYSQL_PATH,
        command_timeout=command_timeout,
        validate_database_exists=validate_database_exists,
        validate_database_has_objects=validate_database_has_objects,
        validate_dump_content=validate_dump_content,
        cleanup_stale_temp_files=cleanup_stale_temp_files,
        stale_temp_file_age_seconds=stale_temp_file_age_seconds,
        single_transaction=single_transaction,
        routines=routines,
        triggers=triggers,
        events=events,
        add_drop_database=add_drop_database,
        add_drop_table=add_drop_table,
        create_options=create_options,
        lock_tables=lock_tables,
        flush_logs=flush_logs,
        master_data=master_data,
        set_gtid_purged=set_gtid_purged,
        where=where,
        ignore_tables=list(ignore_tables or []),
        extra_options=_dump_extra_options(
            extra_options=extra_options,
            include_database_statements=include_database_statements,
            quick=quick,
            hex_blob=hex_blob,
        ),
        compress=compress,
        compression_format=_DEFAULT_COMPRESSION_FORMAT,
        generate_checksum=generate_checksum,
        checksum_algorithm=_DEFAULT_CHECKSUM_ALGORITHM,
        overwrite=overwrite,
    )
    return MySQLBackupManager(connection, dump, retention=retention, logger=logger)


def _backup_artifact(result: BackupResult) -> Path:
    """Return the final artifact path from a successful backup result.

    :param result: ``BackupResult`` returned by a backup operation.
    :return: Compressed file path when present, otherwise uncompressed output path.
    :raises RuntimeError: If the result did not produce an artifact path.
    """

    artifact = result.compressed_file or result.output_file
    if artifact is None:
        raise RuntimeError("Backup succeeded but no output file was returned")
    return artifact


def _backup_failure_message(results: Sequence[BackupResult]) -> str:
    """Return a compact error message for failed backup results.

    :param results: Backup results from one helper call.
    :return: Human-readable message containing database names, errors, and stderr when available.
    """

    parts: list[str] = []
    for result in results:
        if result.success:
            continue
        detail = (
            f"`{result.database}`: {result.error or 'backup returned success=False'}"
        )
        if result.stderr:
            detail = f"{detail}\nSTDERR: {result.stderr}"
        parts.append(detail)
    return "Backup failed: " + "; ".join(parts)


def backup(
    *,
    user: str,
    backup_dir: Path | str,
    database: DatabaseSelection,
    temp_dir: Path | str | None = None,
    host: str = "localhost",
    port: int = 3306,
    password: str | None = None,
    socket: str | None = None,
    command_timeout: float | None = None,
    validate_database_exists: bool = True,
    validate_database_has_objects: bool = True,
    validate_dump_content: bool = True,
    cleanup_stale_temp_files: bool = True,
    stale_temp_file_age_seconds: float | None = 24 * 60 * 60,
    single_transaction: bool = True,
    routines: bool = True,
    triggers: bool = True,
    events: bool = True,
    add_drop_database: bool = False,
    add_drop_table: bool = True,
    create_options: bool = True,
    lock_tables: bool = False,
    flush_logs: bool = False,
    master_data: int | None = None,
    set_gtid_purged: GtidPurgedValue | None = None,
    where: str | None = None,
    include_database_statements: bool = False,
    quick: bool = True,
    hex_blob: bool = False,
    ignore_tables: Sequence[str] | None = None,
    extra_options: Sequence[str] | None = None,
    compress: bool = False,
    generate_checksum: bool = True,
    overwrite: bool = False,
    raise_on_failure: bool = True,
    return_results: bool = False,
    logger: logging.Logger | None = None,
) -> BackupReturn:
    """Run one synchronous backup session using convenient helper parameters.

    The helper uses ``mysqldump`` for dumps and ``mysql`` for preflight checks
    from the current ``PATH``. Use ``DumpConfig`` and ``MySQLBackupManager``
    directly when custom executable paths are required.

    :param user: MySQL user for ``mysqldump`` and mysql preflight validation.
    :param backup_dir: Directory where backup artifacts and checksum sidecars are written.
    :param database: Required backup target. Pass one database name as a string, or multiple names as a sequence of strings.
    :param temp_dir: Optional directory for active hidden ``.part`` staging files. Use this when the default ``~/.MysqlBackupManager`` is not on suitable storage.
    :param host: MySQL host.
    :param port: MySQL port.
    :param password: Optional MySQL password. Passed through ``MYSQL_PWD`` and never command args.
    :param socket: Optional Unix socket path.
    :param command_timeout: Optional maximum runtime in seconds for each dump command.
    :param validate_database_exists: Verify database visibility before dumping.
    :param validate_database_has_objects: Verify visible tables/views before dumping.
    :param validate_dump_content: Verify dump output is not header-only when visible objects exist.
    :param cleanup_stale_temp_files: Best-effort cleanup of stale library-created ``.part`` files before dumping.
    :param stale_temp_file_age_seconds: Minimum age in seconds before stale temp cleanup deletes a file. Use ``None`` to disable age-based deletion.
    :param single_transaction: Add ``--single-transaction``.
    :param routines: Include routines.
    :param triggers: Include triggers.
    :param events: Include events.
    :param add_drop_database: Add ``--add-drop-database``.
    :param add_drop_table: Add ``--add-drop-table``.
    :param create_options: Preserve MySQL create options.
    :param lock_tables: Add ``--lock-tables`` when true; otherwise ``--skip-lock-tables``.
    :param flush_logs: Add ``--flush-logs``.
    :param master_data: Optional ``--master-data`` value.
    :param set_gtid_purged: Optional ``--set-gtid-purged`` value: ``"AUTO"``, ``"ON"``, or ``"OFF"``.
    :param where: Optional ``--where`` condition for partial dumps.
    :param include_database_statements: Add ``--databases`` so the dump includes ``CREATE DATABASE`` and ``USE`` statements. Use this when you want restore to select the database from the file without passing ``target_database``.
    :param quick: Add ``--quick`` to mysqldump options. Defaults to true for streaming large tables efficiently.
    :param hex_blob: Add ``--hex-blob`` to dump binary string columns using hexadecimal notation.
    :param ignore_tables: Optional ``db.table`` entries to skip.
    :param extra_options: Additional raw mysqldump options appended before each database name.
    :param compress: Produce ``.sql.gz`` when true.
    :param generate_checksum: Write a SHA-256 checksum sidecar when true.
    :param overwrite: Replace existing final backup artifacts when true.
    :param raise_on_failure: Raise ``RuntimeError`` when any backup fails. Set false only with ``return_results=True``.
    :param return_results: Return ``BackupResult`` models instead of final artifact paths.
    :param logger: Optional logger for backup lifecycle messages.
    :return: For one database, a ``Path`` by default or ``BackupResult`` when ``return_results=True``. For multiple databases, a list of those values.
    :raises ValueError: If ``database`` is explicitly ``None`` or empty, or if ``raise_on_failure=False`` is used without ``return_results=True``.
    :raises RuntimeError: If a backup fails while ``raise_on_failure=True`` or a successful result has no artifact path.

    ## Example:
    ```python
    backup_file = backup(
        user="backup_user",
        password="secret",
        host="db.example.com",
        database="app",
        backup_dir=Path("~/backups"),
        compress=True,
        hex_blob=True,
        command_timeout=3600,
    )
    ```
    """

    if not raise_on_failure and not return_results:
        raise ValueError("return_results=True is required when raise_on_failure=False")

    database_names = _normalize_databases(database)
    manager = _build_backup_manager(
        databases=database_names,
        backup_dir=backup_dir,
        temp_dir=temp_dir,
        host=host,
        port=port,
        user=user,
        password=password,
        socket=socket,
        command_timeout=command_timeout,
        validate_database_exists=validate_database_exists,
        validate_database_has_objects=validate_database_has_objects,
        validate_dump_content=validate_dump_content,
        cleanup_stale_temp_files=cleanup_stale_temp_files,
        stale_temp_file_age_seconds=stale_temp_file_age_seconds,
        single_transaction=single_transaction,
        routines=routines,
        triggers=triggers,
        events=events,
        add_drop_database=add_drop_database,
        add_drop_table=add_drop_table,
        create_options=create_options,
        lock_tables=lock_tables,
        flush_logs=flush_logs,
        master_data=master_data,
        set_gtid_purged=set_gtid_purged,
        where=where,
        include_database_statements=include_database_statements,
        quick=quick,
        hex_blob=hex_blob,
        ignore_tables=ignore_tables,
        extra_options=extra_options,
        compress=compress,
        generate_checksum=generate_checksum,
        overwrite=overwrite,
        logger=logger,
    )

    results = (
        [manager.backup_database_sync(database_names[0])]
        if len(database_names) == 1
        else manager.backup_all_sync()
    )
    failed_results = [result for result in results if not result.success]
    if failed_results and raise_on_failure:
        raise RuntimeError(_backup_failure_message(results))

    if return_results:
        return results[0] if len(results) == 1 else results

    artifacts = [_backup_artifact(result) for result in results]
    return artifacts[0] if len(artifacts) == 1 else artifacts


def restore(
    *,
    user: str,
    backup_file: Path | str,
    target_database: str | None = None,
    create_database_if_missing: bool = False,
    host: str = "localhost",
    port: int = 3306,
    password: str | None = None,
    socket: str | None = None,
    command_timeout: float | None = None,
    strip_gtid_purged: bool = False,
    force: bool = False,
    extra_options: Sequence[str] | None = None,
    decompress: bool = True,
    verify_checksum_before_restore: bool = False,
    raise_on_failure: bool = True,
    return_result: bool = False,
    logger: logging.Logger | None = None,
) -> RestoreReturn:
    """Run one synchronous restore session using convenient helper parameters.

    The helper streams input into ``mysql`` from the current ``PATH``. Pass
    ``target_database`` when restoring a plain dump that does not contain its own
    ``USE`` statement, or leave it as ``None`` for dumps created with
    ``include_database_statements=True``.

    :param user: MySQL user for the target restore server.
    :param backup_file: SQL or SQL.GZ backup file to stream into mysql. ``~`` is expanded.
    :param target_database: Optional database selected for restore. Use this for plain dumps that do not contain ``CREATE DATABASE`` or ``USE`` statements. When omitted, the dump must select its own database.
    :param create_database_if_missing: When true, inject ``CREATE DATABASE IF NOT EXISTS`` and ``USE`` for ``target_database`` before streaming the dump. Requires ``target_database``.
    :param host: Target MySQL host.
    :param port: Target MySQL port.
    :param password: Optional MySQL password. Passed through ``MYSQL_PWD`` and never command args.
    :param socket: Optional Unix socket path.
    :param command_timeout: Optional maximum runtime in seconds for the restore command.
    :param strip_gtid_purged: Remove ``@@GLOBAL.GTID_PURGED`` statements while streaming restore input.
    :param force: Add ``--force`` so mysql continues after SQL errors.
    :param extra_options: Additional raw mysql options appended to the mysql command.
    :param decompress: Decompress ``.sql.gz`` input while streaming when true.
    :param verify_checksum_before_restore: Verify the adjacent SHA-256 sidecar before restore.
    :param raise_on_failure: Raise ``RuntimeError`` when restore fails. Set false only with ``return_result=True``.
    :param return_result: Return the ``RestoreResult`` model instead of ``None``.
    :param logger: Optional logger for restore lifecycle messages.
    :return: ``None`` by default, or ``RestoreResult`` when ``return_result=True``.
    :raises ValueError: If ``raise_on_failure=False`` is used without ``return_result=True``, or if ``create_database_if_missing=True`` is used without ``target_database``.
    :raises RuntimeError: If checksum verification fails or restore fails while ``raise_on_failure=True``.

    ## Example:
    ```python
    restore(
        user="restore_user",
        password="secret",
        host="db.example.com",
        backup_file=Path("~/backups/app.sql.gz"),
        target_database="app",
        create_database_if_missing=True,
        verify_checksum_before_restore=True,
    )
    ```
    """

    if not raise_on_failure and not return_result:
        raise ValueError("return_result=True is required when raise_on_failure=False")
    if create_database_if_missing and not target_database:
        raise ValueError("target_database is required when create_database_if_missing=True")

    resolved_backup_file = Path(backup_file).expanduser()
    if verify_checksum_before_restore:
        verify_checksum(resolved_backup_file, _DEFAULT_CHECKSUM_ALGORITHM)

    connection = _build_connection_config(
        host=host,
        port=port,
        user=user,
        password=password,
        socket=socket,
    )
    config = RestoreConfig(
        database=target_database,
        input_file=resolved_backup_file,
        mysql_path=_DEFAULT_MYSQL_PATH,
        command_timeout=command_timeout,
        create_database_if_missing=create_database_if_missing,
        strip_gtid_purged=strip_gtid_purged,
        force=force,
        extra_options=list(extra_options or []),
        decompress=decompress,
    )
    ensure_no_running_loop()
    result = asyncio.run(RestoreService(connection, config, logger=logger).restore())

    if not result.success and raise_on_failure:
        message = f"Restore failed: {result.error}"
        stderr = result.stderr or ""
        if "No database selected" in stderr and target_database is None:
            message = (
                f"{message}. The dump does not appear to select a database; "
                "pass target_database='your_db' or create future backups with "
                "include_database_statements=True."
            )
        if result.stderr:
            message = f"{message}\nSTDERR: {result.stderr}"
        raise RuntimeError(message)

    return result if return_result else None


def scheduled_backup(
    *,
    user: str,
    backup_dir: Path | str,
    database: DatabaseSelection,
    temp_dir: Path | str | None = None,
    host: str = "localhost",
    port: int = 3306,
    password: str | None = None,
    socket: str | None = None,
    command_timeout: float | None = None,
    validate_database_exists: bool = True,
    validate_database_has_objects: bool = True,
    validate_dump_content: bool = True,
    cleanup_stale_temp_files: bool = True,
    stale_temp_file_age_seconds: float | None = 24 * 60 * 60,
    single_transaction: bool = True,
    routines: bool = True,
    triggers: bool = True,
    events: bool = True,
    add_drop_database: bool = False,
    add_drop_table: bool = True,
    create_options: bool = True,
    lock_tables: bool = False,
    flush_logs: bool = False,
    master_data: int | None = None,
    set_gtid_purged: GtidPurgedValue | None = None,
    where: str | None = None,
    include_database_statements: bool = False,
    quick: bool = True,
    hex_blob: bool = False,
    ignore_tables: Sequence[str] | None = None,
    extra_options: Sequence[str] | None = None,
    compress: bool = False,
    generate_checksum: bool = True,
    overwrite: bool = False,
    enabled: bool = True,
    interval_seconds: int | None = None,
    cron: str | None = None,
    timezone: str = "UTC",
    run_immediately: bool = False,
    stop_on_failure: bool = True,
    retention_enabled: bool = True,
    keep_last: int | None = 10,
    keep_days: int | None = 30,
    match_pattern: str = "*.sql*",
    logger: logging.Logger | None = None,
) -> None:
    """Run configurable backups forever on an interval or cron schedule.

    Scheduled helper runs use the same fixed native executable defaults as
    ``backup()``. Use ``SchedulerService`` with explicit config models when
    custom executable paths are required.

    :param user: MySQL user for ``mysqldump`` and mysql preflight validation.
    :param backup_dir: Directory where backup artifacts and checksum sidecars are written.
    :param database: Required scheduled backup target. Pass one database name as a string, or multiple names as a sequence of strings.
    :param temp_dir: Optional directory for active hidden ``.part`` staging files. Use this when the default ``~/.MysqlBackupManager`` is not on suitable storage.
    :param host: MySQL host.
    :param port: MySQL port.
    :param password: Optional MySQL password. Passed through ``MYSQL_PWD`` and never command args.
    :param socket: Optional Unix socket path.
    :param command_timeout: Optional maximum runtime in seconds for each dump command.
    :param validate_database_exists: Verify database visibility before dumping.
    :param validate_database_has_objects: Verify visible tables/views before dumping.
    :param validate_dump_content: Verify dump output is not header-only when visible objects exist.
    :param cleanup_stale_temp_files: Best-effort cleanup of stale library-created ``.part`` files before dumping.
    :param stale_temp_file_age_seconds: Minimum age in seconds before stale temp cleanup deletes a file. Use ``None`` to disable age-based deletion.
    :param single_transaction: Add ``--single-transaction``.
    :param routines: Include routines.
    :param triggers: Include triggers.
    :param events: Include events.
    :param add_drop_database: Add ``--add-drop-database``.
    :param add_drop_table: Add ``--add-drop-table``.
    :param create_options: Preserve MySQL create options.
    :param lock_tables: Add ``--lock-tables`` when true; otherwise ``--skip-lock-tables``.
    :param flush_logs: Add ``--flush-logs``.
    :param master_data: Optional ``--master-data`` value.
    :param set_gtid_purged: Optional ``--set-gtid-purged`` value: ``"AUTO"``, ``"ON"``, or ``"OFF"``.
    :param where: Optional ``--where`` condition for partial dumps.
    :param include_database_statements: Add ``--databases`` so scheduled dumps include ``CREATE DATABASE`` and ``USE`` statements. Use this when scheduled backups should restore without passing ``target_database``.
    :param quick: Add ``--quick`` to mysqldump options. Defaults to true for streaming large tables efficiently.
    :param hex_blob: Add ``--hex-blob`` to dump binary string columns using hexadecimal notation.
    :param ignore_tables: Optional ``db.table`` entries to skip.
    :param extra_options: Additional raw mysqldump options appended before each database name.
    :param compress: Produce ``.sql.gz`` when true.
    :param generate_checksum: Write a SHA-256 checksum sidecar when true.
    :param overwrite: Replace existing final backup artifacts when true.
    :param enabled: Whether the scheduler should run. When false, the helper returns immediately.
    :param interval_seconds: Fixed interval between runs. Mutually exclusive with ``cron``.
    :param cron: Cron expression such as ``"0 2 * * *"``. Mutually exclusive with ``interval_seconds``.
    :param timezone: IANA timezone used for cron evaluation.
    :param run_immediately: Run one backup before waiting for the first scheduled time.
    :param stop_on_failure: Raise and stop the loop when a scheduled cycle fails.
    :param retention_enabled: Enable retention cleanup after successful scheduled backups.
    :param keep_last: Delete matching backup artifacts beyond this newest-file count. Set ``None`` to disable.
    :param keep_days: Delete matching backup artifacts older than this many days. Set ``None`` to disable.
    :param match_pattern: Retention glob pattern inside ``backup_dir``.
    :param logger: Optional logger for scheduler, backup, and retention messages.
    :return: None. Enabled schedules run until interrupted or cancelled.
    :raises ValueError: If ``database`` is explicitly ``None`` or empty.

    ## Example:
    ```python
    scheduled_backup(
        user="backup_user",
        password="secret",
        database="app",
        backup_dir=Path("~/backups"),
        cron="0 2 * * *",
        timezone="Asia/Shanghai",
        compress=True,
        hex_blob=True,
        keep_last=7,
        keep_days=30,
    )
    ```
    """

    database_names = _normalize_databases(database)
    retention = RetentionConfig(
        enabled=retention_enabled,
        keep_last=keep_last,
        keep_days=keep_days,
        match_pattern=match_pattern,
    )
    manager = _build_backup_manager(
        databases=database_names,
        backup_dir=backup_dir,
        temp_dir=temp_dir,
        host=host,
        port=port,
        user=user,
        password=password,
        socket=socket,
        command_timeout=command_timeout,
        validate_database_exists=validate_database_exists,
        validate_database_has_objects=validate_database_has_objects,
        validate_dump_content=validate_dump_content,
        cleanup_stale_temp_files=cleanup_stale_temp_files,
        stale_temp_file_age_seconds=stale_temp_file_age_seconds,
        single_transaction=single_transaction,
        routines=routines,
        triggers=triggers,
        events=events,
        add_drop_database=add_drop_database,
        add_drop_table=add_drop_table,
        create_options=create_options,
        lock_tables=lock_tables,
        flush_logs=flush_logs,
        master_data=master_data,
        set_gtid_purged=set_gtid_purged,
        where=where,
        include_database_statements=include_database_statements,
        quick=quick,
        hex_blob=hex_blob,
        ignore_tables=ignore_tables,
        extra_options=extra_options,
        compress=compress,
        generate_checksum=generate_checksum,
        overwrite=overwrite,
        retention=retention,
        logger=logger,
    )
    scheduler = SchedulerService(
        manager=manager,
        config=ScheduleConfig(
            enabled=enabled,
            interval_seconds=interval_seconds,
            cron=cron,
            timezone=timezone,
            run_immediately=run_immediately,
        ),
        logger=logger,
    )

    try:
        ensure_no_running_loop()
        asyncio.run(scheduler.run_forever(stop_on_failure=stop_on_failure))
    except KeyboardInterrupt:
        (logger or globals()["logger"]).info(
            "Scheduled backup interrupted by user; exiting cleanly"
        )
