"""Backup service and high-level orchestration API.

This module contains the low-level ``BackupService`` that builds and executes
``mysqldump`` commands and the high-level ``MySQLBackupManager`` facade used by
applications. The manager combines backup, restore, retention, and sync
convenience APIs around one shared MySQL connection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mysql_backup_manager.checksum import write_checksum_file
from mysql_backup_manager.compression import gzip_file
from mysql_backup_manager.config import DumpConfig, MySQLConnectionConfig, RestoreConfig, RetentionConfig
from mysql_backup_manager.exceptions import MySQLBackupError, MySQLClientNotFoundError, MySQLCommandError
from mysql_backup_manager.logging import get_logger
from mysql_backup_manager.models import BackupResult, RestoreResult, RetentionResult
from mysql_backup_manager.process import build_env, run_command_capture, run_command_to_file
from mysql_backup_manager.utils import elapsed_seconds, ensure_no_running_loop, utc_now


def quote_mysql_string_literal(value: str) -> str:
    """Quote a value as a MySQL string literal for preflight SQL.

    :param value: Raw string value to quote.
    :return: Single-quoted SQL string with quotes and backslashes escaped.

    ## Example:
    ```python
    quote_mysql_string_literal("app's db")
    # "'app''s db'"
    ```
    """

    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def dump_file_contains_database_objects(path: Path) -> bool:
    """Return whether an uncompressed dump contains table/view definitions or rows.

    :param path: Uncompressed SQL dump file to inspect.
    :return: ``True`` when the dump includes common MySQL dump markers for table definitions, view definitions, or inserted row data.

    ## Example:
    ```python
    # dump_file_contains_database_objects(Path("backup.sql"))
    ```
    """

    markers = (
        b"CREATE TABLE ",
        b"CREATE TEMPORARY TABLE ",
        b"CREATE VIEW ",
        b"CREATE ALGORITHM",
        b"INSERT INTO ",
    )
    overlap = max(len(marker) for marker in markers) - 1
    previous = b""
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            window = previous + chunk.upper()
            if any(marker in window for marker in markers):
                return True
            previous = window[-overlap:]
    return False


def is_backup_temp_file(path: Path) -> bool:
    """Return whether ``path`` looks like a library-created backup temp file.

    :param path: Candidate path inside a backup output directory.
    :return: ``True`` for hidden ``.part`` files whose name matches the library temp-file shape ``.<sql-name>.<uuid>.part``; otherwise ``False``. Symlinks are never treated as backup temp files.

    ## Example:
    ```python
    is_backup_temp_file(Path(".app_20260101.sql.0123456789abcdef0123456789abcdef.part"))
    # True
    ```
    """

    if path.is_symlink():
        return False
    name = path.name
    if not name.startswith(".") or not name.endswith(".part"):
        return False
    parts = name.split(".")
    if len(parts) < 5 or "sql" not in parts[1:-2]:
        return False
    uuid_token = parts[-2]
    return len(uuid_token) == 32 and all(
        character in "0123456789abcdef" for character in uuid_token.lower()
    )


def cleanup_stale_backup_temp_files(
    output_dir: Path,
    *,
    older_than_seconds: float | None,
    logger: logging.Logger | None = None,
) -> list[Path]:
    """Remove stale hidden ``.part`` backup files from ``output_dir``.

    :param output_dir: Backup directory to inspect. Only direct child files are considered.
    :param older_than_seconds: Minimum file age in seconds before deletion. Pass ``0`` to delete matching files immediately, or ``None`` to disable deletion.
    :param logger: Optional logger used to report deleted temp files.
    :return: List of temp files that were removed.
    :raises OSError: If the directory cannot be scanned or a matching file cannot be deleted.

    ## Example:
    ```python
    deleted = cleanup_stale_backup_temp_files(Path("./backups"), older_than_seconds=86400)
    ```
    """

    if older_than_seconds is None:
        return []

    directory = Path(output_dir).expanduser()
    if not directory.exists():
        return []

    deleted_files: list[Path] = []
    now = time.time()
    for path in directory.iterdir():
        if not is_backup_temp_file(path) or not path.is_file():
            continue
        age_seconds = now - path.stat().st_mtime
        if age_seconds < older_than_seconds:
            continue
        path.unlink()
        deleted_files.append(path)
        if logger is not None:
            logger.info("Removed stale backup temp file: %s", path)
    return deleted_files


def _new_backup_temp_file(final_path: Path) -> Path:
    """Return a unique hidden temp path next to ``final_path``.

    :param final_path: Intended final backup artifact path.
    :return: Hidden ``.part`` path in the same directory, suitable for atomic replacement of ``final_path``.
    """

    return final_path.with_name(f".{final_path.name}.{uuid4().hex}.part")


class BackupService:
    """Service responsible for one-database backup execution.

    :param connection: Shared MySQL connection settings used to build client commands and pass the password safely through the subprocess environment.
    :param config: Dump settings that control command options, file naming, compression, checksum generation, and timeout behavior.
    :param logger: Optional logger for backup lifecycle messages.
    :return: A ``BackupService`` instance. Construct it directly for command-builder unit tests or advanced integrations; most applications should use ``MySQLBackupManager``.

    ## Example:
    ```python
    from pathlib import Path
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=["app"], output_dir=Path("./backups")),
    )
    service.build_command("app")[-1]
    # 'app'
    ```
    """

    def __init__(
        self,
        connection: MySQLConnectionConfig,
        config: DumpConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a backup service for a connection and dump configuration.

        :param connection: Validated ``MySQLConnectionConfig`` used for host, port, user, socket, charset, and password environment handling.
        :param config: Validated ``DumpConfig`` used for command flags and output behavior.
        :param logger: Optional logger. When omitted, the package logger is used.
        :return: None. The constructor stores dependencies only and does not start a process.
        """

        self.connection = connection
        self.config = config
        self.logger = logger or get_logger(__name__)

    def build_command(self, database: str) -> list[str]:
        """Build the ``mysqldump`` argument vector for one database.

        :param database: Database name to append as the final command argument.
        :return: A sanitized command list suitable for ``asyncio.create_subprocess_exec``. The password is never included; it is passed later through ``MYSQL_PWD``.

        ## Example:
        ```python
        from pathlib import Path
        service = BackupService(
            MySQLConnectionConfig(host="db", user="backup", password="secret"),
            DumpConfig(databases=["app"], output_dir=Path("./backups")),
        )
        "--user=backup" in service.build_command("app")
        # True
        ```
        """

        command = [
            self.config.mysqldump_path,
            f"--host={self.connection.host}",
            f"--port={self.connection.port}",
            f"--user={self.connection.user}",
        ]
        if self.connection.socket:
            command.append(f"--socket={self.connection.socket}")
        if self.connection.default_character_set:
            command.append(f"--default-character-set={self.connection.default_character_set}")
        if self.config.single_transaction:
            command.append("--single-transaction")
        if self.config.routines:
            command.append("--routines")
        if self.config.triggers:
            command.append("--triggers")
        if self.config.events:
            command.append("--events")
        if self.config.add_drop_database:
            command.append("--add-drop-database")
        if self.config.add_drop_table:
            command.append("--add-drop-table")
        if not self.config.create_options:
            command.append("--no-create-options")
        if self.config.lock_tables:
            command.append("--lock-tables")
        else:
            command.append("--skip-lock-tables")
        if self.config.flush_logs:
            command.append("--flush-logs")
        if self.config.master_data is not None:
            command.append(f"--master-data={self.config.master_data}")
        if self.config.set_gtid_purged is not None:
            command.append(f"--set-gtid-purged={self.config.set_gtid_purged}")
        if self.config.where:
            command.append(f"--where={self.config.where}")
        for table in self.config.ignore_tables:
            command.append(f"--ignore-table={table}")
        command.extend(self.config.extra_options)
        command.append(database)
        return command

    def build_database_exists_command(self, database: str) -> list[str]:
        """Build a ``mysql`` preflight command that checks database visibility.

        :param database: Database name to verify before running ``mysqldump``.
        :return: Sanitized ``mysql`` command that prints ``1`` when the database exists and is visible, otherwise ``0``. Passwords are never included.

        ## Example:
        ```python
        command = service.build_database_exists_command("app")
        command[-1].startswith("--execute=SELECT EXISTS")
        # True
        ```
        """

        query = (
            "SELECT EXISTS("
            "SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA "
            f"WHERE SCHEMA_NAME = {quote_mysql_string_literal(database)}"
            ")"
        )
        command = [
            self.config.mysql_path,
            f"--host={self.connection.host}",
            f"--port={self.connection.port}",
            f"--user={self.connection.user}",
            "--batch",
            "--skip-column-names",
        ]
        if self.connection.socket:
            command.append(f"--socket={self.connection.socket}")
        if self.connection.default_character_set:
            command.append(f"--default-character-set={self.connection.default_character_set}")
        if self.connection.connect_timeout is not None:
            command.append(f"--connect-timeout={self.connection.connect_timeout}")
        command.append(f"--execute={query}")
        return command

    def _database_exists_timeout(self) -> float | None:
        """Return the timeout used by the database-existence preflight.

        :return: A bounded timeout in seconds when a connection timeout or command timeout is configured, otherwise ``None``.
        """

        if self.connection.connect_timeout is not None:
            timeout = float(self.connection.connect_timeout + 5)
            if self.config.command_timeout is not None:
                return min(float(self.config.command_timeout), timeout)
            return timeout
        return self.config.command_timeout

    async def database_exists(self, database: str) -> bool:
        """Return whether ``database`` exists and is visible to the connection.

        :param database: Database name to check through the native ``mysql`` client.
        :return: ``True`` when ``INFORMATION_SCHEMA.SCHEMATA`` reports the database, otherwise ``False``.
        :raises MySQLCommandError: If the ``mysql`` preflight command fails.
        :raises MySQLBackupError: If the preflight returns an unexpected response.
        """

        stdout, _ = await run_command_capture(
            self.build_database_exists_command(database),
            env=build_env(self.connection.password_value()),
            timeout=self._database_exists_timeout(),
        )
        answer = stdout.strip()
        if answer == "1":
            return True
        if answer == "0":
            return False
        raise MySQLBackupError(
            f"Unexpected database-existence check response for `{database}`: {answer!r}"
        )

    def build_database_object_count_command(self, database: str) -> list[str]:
        """Build a ``mysql`` preflight command that counts visible tables/views.

        :param database: Database name whose visible tables and views should be counted.
        :return: Sanitized ``mysql`` command that prints the number of visible ``INFORMATION_SCHEMA.TABLES`` rows for the database. Passwords are never included.

        ## Example:
        ```python
        command = service.build_database_object_count_command("app")
        command[-1].startswith("--execute=SELECT COUNT")
        # True
        ```
        """

        query = (
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            f"WHERE TABLE_SCHEMA = {quote_mysql_string_literal(database)}"
        )
        command = [
            self.config.mysql_path,
            f"--host={self.connection.host}",
            f"--port={self.connection.port}",
            f"--user={self.connection.user}",
            "--batch",
            "--skip-column-names",
        ]
        if self.connection.socket:
            command.append(f"--socket={self.connection.socket}")
        if self.connection.default_character_set:
            command.append(f"--default-character-set={self.connection.default_character_set}")
        if self.connection.connect_timeout is not None:
            command.append(f"--connect-timeout={self.connection.connect_timeout}")
        command.append(f"--execute={query}")
        return command

    async def visible_object_count(self, database: str) -> int:
        """Return how many tables or views are visible to the connection.

        :param database: Database name to inspect through ``INFORMATION_SCHEMA.TABLES``.
        :return: Number of visible tables and views for the configured user.
        :raises MySQLCommandError: If the ``mysql`` preflight command fails.
        :raises MySQLBackupError: If the preflight returns a non-integer response.
        """

        stdout, _ = await run_command_capture(
            self.build_database_object_count_command(database),
            env=build_env(self.connection.password_value()),
            timeout=self._database_exists_timeout(),
        )
        answer = stdout.strip()
        try:
            return int(answer)
        except ValueError as exc:
            raise MySQLBackupError(
                f"Unexpected visible-object count response for `{database}`: {answer!r}"
            ) from exc

    def build_output_path(self, database: str, *, now: datetime | None = None) -> Path:
        """Return the final uncompressed ``.sql`` path for a backup.

        :param database: Database name used to render ``DumpConfig.filename_template``.
        :param now: Optional timezone-aware timestamp. Tests can pass this to make the generated filename deterministic.
        :return: A ``Path`` inside ``DumpConfig.output_dir`` ending in the template-rendered uncompressed SQL filename.
        :raises MySQLBackupError: If the rendered template tries to create an absolute path or a path containing directories.

        ## Example:
        ```python
        from datetime import datetime, timezone
        from pathlib import Path
        service = BackupService(MySQLConnectionConfig(user="root"), DumpConfig(databases=["app"], output_dir=Path("./backups")))
        service.build_output_path("app", now=datetime(2026, 1, 1, tzinfo=timezone.utc)).name
        # 'app_20260101_000000.sql'
        ```
        """

        timestamp = (now or utc_now()).strftime(self.config.timestamp_format)
        filename = self.config.filename_template.format(database=database, timestamp=timestamp)
        rendered = Path(filename)
        if rendered.name != filename or rendered.is_absolute():
            raise MySQLBackupError("filename_template must produce a plain filename inside output_dir")
        return self.config.output_dir / rendered

    async def backup_database(self, database: str) -> BackupResult:
        """Back up one database and return a structured result.

        :param database: Non-empty database name to dump. The low-level service does not require this name to appear in ``DumpConfig.databases``; the manager facade enforces that higher-level guard.
        :return: ``BackupResult`` containing success status, output paths, checksum details, elapsed time, sanitized command arguments, stderr, and any error message.
        :raises MySQLBackupError: If ``database`` is blank. Native command failures are captured in the returned result instead of being raised.

        ## Example:
        ```python
        # In async application code:
        # result = await service.backup_database("app")
        # if result.success: print(result.compressed_file or result.output_file)
        ```
        """

        database = database.strip()
        if not database:
            raise MySQLBackupError("database must not be empty")
        if "\x00" in database:
            raise MySQLBackupError("database must not contain null bytes")

        started_at = utc_now()
        output_file = self.build_output_path(database, now=started_at)
        temp_output_file = _new_backup_temp_file(output_file)
        command = self.build_command(database)
        compressed_file: Path | None = None
        checksum_file: Path | None = None
        checksum: str | None = None
        stderr: str | None = None
        error: str | None = None
        success = False
        visible_objects: int | None = None

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        if self.config.cleanup_stale_temp_files:
            try:
                cleanup_stale_backup_temp_files(
                    self.config.output_dir,
                    older_than_seconds=self.config.stale_temp_file_age_seconds,
                    logger=self.logger,
                )
            except OSError as exc:
                self.logger.warning("Failed to remove stale backup temp files: %s", exc)
        expected_final_file = output_file.with_suffix(output_file.suffix + ".gz") if self.config.compress else output_file
        collision_candidates = [expected_final_file]
        if self.config.compress:
            collision_candidates.append(output_file)
        existing_output = next((path for path in collision_candidates if path.exists()), None)
        if existing_output is not None and not self.config.overwrite:
            error = f"Output file already exists: {existing_output}"
            finished_at = utc_now()
            return BackupResult(
                database=database,
                success=False,
                output_file=output_file,
                compressed_file=expected_final_file if expected_final_file.exists() else None,
                checksum_file=None,
                checksum=None,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed_seconds(started_at, finished_at),
                file_size_bytes=existing_output.stat().st_size,
                command=command,
                stderr=None,
                error=error,
            )

        try:
            if self.config.validate_database_exists:
                self.logger.info("Validating database `%s` exists before backup", database)
                if not await self.database_exists(database):
                    raise MySQLBackupError(
                        f"Database `{database}` does not exist or is not visible to user `{self.connection.user}`"
                    )
            if self.config.validate_database_has_objects or self.config.validate_dump_content:
                visible_objects = await self.visible_object_count(database)
            if self.config.validate_database_has_objects and visible_objects == 0:
                raise MySQLBackupError(
                    f"Database `{database}` has no visible tables or views for user `{self.connection.user}`; "
                    "verify backup grants or set validate_database_has_objects=False for an intentionally empty database"
                )

            self.logger.info("Starting backup for database `%s`", database)
            stderr = await run_command_to_file(
                command,
                temp_output_file,
                env=build_env(self.connection.password_value()),
                timeout=self.config.command_timeout,
            )
            if (
                self.config.validate_dump_content
                and visible_objects is not None
                and visible_objects > 0
                and not await asyncio.to_thread(
                    dump_file_contains_database_objects, temp_output_file
                )
            ):
                raise MySQLBackupError(
                    f"mysqldump for database `{database}` completed but produced no table definitions or row data; "
                    "verify mysqldump options and backup user privileges"
                )
            temp_output_file.replace(output_file)
            final_file = output_file
            if self.config.compress:
                compressed_file = await asyncio.to_thread(
                    gzip_file, output_file, remove_original=True
                )
                final_file = compressed_file
            if self.config.generate_checksum:
                checksum_file, checksum = await asyncio.to_thread(
                    write_checksum_file, final_file, self.config.checksum_algorithm
                )
            success = True
            self.logger.info("Backup succeeded for database `%s`: %s", database, final_file)
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.logger.warning("Backup interrupted for database `%s`; cleaning up temporary files", database)
            raise
        except MySQLCommandError as exc:
            stderr = exc.stderr
            error = str(exc)
            self.logger.error(
                "Backup failed for database `%s`: %s%s",
                database,
                error,
                f"; stderr: {stderr.strip()}" if stderr and stderr.strip() else "",
            )
        except (MySQLBackupError, MySQLClientNotFoundError) as exc:
            error = str(exc)
            self.logger.error("Backup failed for database `%s`: %s", database, error)
        except Exception as exc:
            error = str(exc)
            self.logger.exception("Backup failed for database `%s`", database)
        finally:
            temp_output_file.unlink(missing_ok=True)

        finished_at = utc_now()
        final_path = compressed_file or output_file
        return BackupResult(
            database=database,
            success=success,
            output_file=output_file,
            compressed_file=compressed_file,
            checksum_file=checksum_file,
            checksum=checksum,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds(started_at, finished_at),
            file_size_bytes=final_path.stat().st_size if final_path.exists() else None,
            command=command,
            stderr=stderr,
            error=error,
        )


class MySQLBackupManager:
    """Facade that coordinates backup, restore, and retention workflows.

    :param connection: Shared ``MySQLConnectionConfig`` for both backup and restore client commands.
    :param dump: ``DumpConfig`` defining the backup workspace and allowed databases.
    :param retention: Optional ``RetentionConfig``. Defaults to enabled retention with the library defaults.
    :param logger: Optional logger used by all services created by the manager.
    :return: A reusable ``MySQLBackupManager`` instance.

    ## Example:
    ```python
    from pathlib import Path
    manager = MySQLBackupManager(
        connection=MySQLConnectionConfig(user="root", password="secret"),
        dump=DumpConfig(databases=["app"], output_dir=Path("./backups"), compress=True),
    )
    manager.dump.databases
    # ['app']
    ```
    """

    def __init__(
        self,
        connection: MySQLConnectionConfig,
        dump: DumpConfig,
        retention: RetentionConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a manager with shared connection, dump, and retention config.

        :param connection: Connection settings for native MySQL client tools.
        :param dump: Backup configuration. Required because the manager owns the backup workspace and retention directory even when only restore methods are used.
        :param retention: Optional retention policy. ``RetentionConfig()`` is used when omitted.
        :param logger: Optional logger shared with backup, restore, and retention services.
        :return: None. The constructor builds service objects but does not run backups.
        """

        self.connection = connection
        self.dump = dump
        self.retention = retention or RetentionConfig()
        self.logger = logger or get_logger(__name__)
        self.backup_service = BackupService(connection, dump, logger=self.logger)

    async def backup_database(self, database: str) -> BackupResult:
        """Back up one configured database asynchronously.

        :param database: Database name to dump. It must be listed in ``DumpConfig.databases`` for this manager.
        :return: ``BackupResult`` with output paths, checksum information, elapsed time, sanitized command, stderr, and error details.
        :raises MySQLBackupError: If ``database`` is not configured for this manager.

        ## Example:
        ```python
        # result = await manager.backup_database("app")
        ```
        """

        database = database.strip()
        if database not in self.dump.databases:
            raise MySQLBackupError(f"Database is not configured for backup: {database}")
        return await self.backup_service.backup_database(database)

    async def backup_all(self) -> list[BackupResult]:
        """Back up every database configured in ``DumpConfig.databases``.

        :return: A list of ``BackupResult`` objects in the same order as ``DumpConfig.databases``.

        ## Example:
        ```python
        # results = await manager.backup_all()
        # assert all(result.success for result in results)
        ```
        """

        return [await self.backup_service.backup_database(database) for database in self.dump.databases]

    async def restore(self, config: RestoreConfig) -> RestoreResult:
        """Restore a SQL or SQL.GZ file with the shared connection.

        :param config: ``RestoreConfig`` for this one restore operation, including the input file, target database behavior, GTID filtering, and timeout.
        :return: ``RestoreResult`` with success status, timing, sanitized command, stderr, and error details.

        ## Example:
        ```python
        # result = await manager.restore(RestoreConfig(database="app", input_file=Path("backup.sql.gz")))
        ```
        """

        from mysql_backup_manager.restore import RestoreService

        return await RestoreService(self.connection, config, logger=self.logger).restore()

    async def cleanup_retention(self) -> RetentionResult:
        """Delete old backup files from ``DumpConfig.output_dir``.

        :return: ``RetentionResult`` listing deleted files, kept files, and any cleanup error.

        ## Example:
        ```python
        # result = await manager.cleanup_retention()
        ```
        """

        from mysql_backup_manager.retention import RetentionService

        return RetentionService(self.dump.output_dir, self.retention, logger=self.logger).cleanup()

    def backup_database_sync(self, database: str) -> BackupResult:
        """Synchronously back up one configured database.

        :param database: Database name to dump. It must be listed in this manager's ``DumpConfig.databases``.
        :return: ``BackupResult`` from the async backup operation.
        :raises RuntimeError: If called while an asyncio event loop is already running.
        :raises MySQLBackupError: If ``database`` is not configured.

        ## Example:
        ```python
        # result = manager.backup_database_sync("app")
        ```
        """

        ensure_no_running_loop()
        return asyncio.run(self.backup_database(database))

    def backup_all_sync(self) -> list[BackupResult]:
        """Synchronously back up all configured databases.

        :return: A list of ``BackupResult`` objects.
        :raises RuntimeError: If called while an asyncio event loop is already running.

        ## Example:
        ```python
        # results = manager.backup_all_sync()
        ```
        """

        ensure_no_running_loop()
        return asyncio.run(self.backup_all())

    def restore_sync(self, config: RestoreConfig) -> RestoreResult:
        """Synchronously restore one SQL or SQL.GZ file.

        :param config: ``RestoreConfig`` describing the input file and restore behavior.
        :return: ``RestoreResult`` from the async restore operation.
        :raises RuntimeError: If called while an asyncio event loop is already running.

        ## Example:
        ```python
        # result = manager.restore_sync(RestoreConfig(database="app", input_file=Path("backup.sql.gz")))
        ```
        """

        ensure_no_running_loop()
        return asyncio.run(self.restore(config))

    def cleanup_retention_sync(self) -> RetentionResult:
        """Synchronously run retention cleanup for the manager output directory.

        :return: ``RetentionResult`` listing deleted and kept files.
        :raises RuntimeError: If called while an asyncio event loop is already running.

        ## Example:
        ```python
        # result = manager.cleanup_retention_sync()
        ```
        """

        ensure_no_running_loop()
        return asyncio.run(self.cleanup_retention())

