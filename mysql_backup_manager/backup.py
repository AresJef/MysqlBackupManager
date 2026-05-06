"""Backup services and high-level manager."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from mysql_backup_manager.checksum import write_checksum_file
from mysql_backup_manager.compression import gzip_file
from mysql_backup_manager.config import DumpConfig, MySQLConnectionConfig, RestoreConfig, RetentionConfig
from mysql_backup_manager.exceptions import MySQLBackupError, MySQLCommandError
from mysql_backup_manager.logging import get_logger
from mysql_backup_manager.models import BackupResult, RestoreResult, RetentionResult
from mysql_backup_manager.process import build_env, run_command_to_file


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def elapsed_seconds(started_at: datetime, finished_at: datetime) -> float:
    """Return elapsed seconds between two aware datetimes."""

    return (finished_at - started_at).total_seconds()


def _ensure_no_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Sync APIs cannot be called from a running event loop; use the async API instead")


class BackupService:
    """Build and execute mysqldump backup commands."""

    def __init__(
        self,
        connection: MySQLConnectionConfig,
        config: DumpConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.connection = connection
        self.config = config
        self.logger = logger or get_logger(__name__)

    def build_command(self, database: str) -> list[str]:
        """Build a sanitized mysqldump command without secrets."""

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
        if self.connection.connect_timeout is not None:
            command.append(f"--connect-timeout={self.connection.connect_timeout}")
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

    def build_output_path(self, database: str, *, now: datetime | None = None) -> Path:
        """Build the output SQL path for a database backup."""

        timestamp = (now or utc_now()).strftime(self.config.timestamp_format)
        filename = self.config.filename_template.format(database=database, timestamp=timestamp)
        rendered = Path(filename)
        if rendered.name != filename or rendered.is_absolute():
            raise MySQLBackupError("filename_template must produce a plain filename inside output_dir")
        return self.config.output_dir / rendered

    async def backup_database(self, database: str) -> BackupResult:
        """Back up one database using mysqldump."""

        database = database.strip()
        if not database:
            raise MySQLBackupError("database must not be empty")

        started_at = utc_now()
        output_file = self.build_output_path(database, now=started_at)
        temp_output_file = output_file.with_name(f".{output_file.name}.{uuid4().hex}.part")
        command = self.build_command(database)
        compressed_file: Path | None = None
        checksum_file: Path | None = None
        checksum: str | None = None
        stderr: str | None = None
        error: str | None = None
        success = False

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        expected_final_file = output_file.with_suffix(output_file.suffix + ".gz") if self.config.compress else output_file
        if expected_final_file.exists() and not self.config.overwrite:
            error = f"Output file already exists: {expected_final_file}"
            finished_at = utc_now()
            return BackupResult(
                database=database,
                success=False,
                output_file=output_file,
                compressed_file=expected_final_file if self.config.compress else None,
                checksum_file=None,
                checksum=None,
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=elapsed_seconds(started_at, finished_at),
                file_size_bytes=expected_final_file.stat().st_size,
                command=command,
                stderr=None,
                error=error,
            )

        try:
            self.logger.info("Starting backup for database %s", database)
            stderr = await run_command_to_file(
                command,
                temp_output_file,
                env=build_env(self.connection.password_value()),
                timeout=self.config.command_timeout,
            )
            temp_output_file.replace(output_file)
            final_file = output_file
            if self.config.compress:
                compressed_file = gzip_file(output_file, remove_original=True)
                final_file = compressed_file
            if self.config.generate_checksum:
                checksum_file, checksum = write_checksum_file(final_file, self.config.checksum_algorithm)
            success = True
            self.logger.info("Backup succeeded for database %s: %s", database, final_file)
        except MySQLCommandError as exc:
            stderr = exc.stderr
            error = str(exc)
            self.logger.exception("Backup failed for database %s", database)
        except Exception as exc:
            error = str(exc)
            self.logger.exception("Backup failed for database %s", database)
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
    """High-level API for backup, restore, and retention cleanup."""

    def __init__(
        self,
        connection: MySQLConnectionConfig,
        dump: DumpConfig,
        retention: RetentionConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.connection = connection
        self.dump = dump
        self.retention = retention or RetentionConfig()
        self.logger = logger or get_logger(__name__)
        self.backup_service = BackupService(connection, dump, logger=self.logger)

    async def backup_database(self, database: str) -> BackupResult:
        """Back up one configured database."""

        database = database.strip()
        if database not in self.dump.databases:
            raise MySQLBackupError(f"Database is not configured for backup: {database}")
        return await self.backup_service.backup_database(database)

    async def backup_all(self) -> list[BackupResult]:
        """Back up all configured databases sequentially."""

        return [await self.backup_service.backup_database(database) for database in self.dump.databases]

    async def restore(self, config: RestoreConfig) -> RestoreResult:
        """Restore SQL using mysql."""

        from mysql_backup_manager.restore import RestoreService

        return await RestoreService(self.connection, config, logger=self.logger).restore()

    async def cleanup_retention(self) -> RetentionResult:
        """Clean up old backups according to retention policy."""

        from mysql_backup_manager.retention import RetentionService

        return RetentionService(self.dump.output_dir, self.retention, logger=self.logger).cleanup()

    def backup_database_sync(self, database: str) -> BackupResult:
        """Synchronous wrapper for backup_database."""

        _ensure_no_running_loop()
        return asyncio.run(self.backup_database(database))

    def backup_all_sync(self) -> list[BackupResult]:
        """Synchronous wrapper for backup_all."""

        _ensure_no_running_loop()
        return asyncio.run(self.backup_all())

    def restore_sync(self, config: RestoreConfig) -> RestoreResult:
        """Synchronous wrapper for restore."""

        _ensure_no_running_loop()
        return asyncio.run(self.restore(config))

    def cleanup_retention_sync(self) -> RetentionResult:
        """Synchronous wrapper for cleanup_retention."""

        _ensure_no_running_loop()
        return asyncio.run(self.cleanup_retention())

