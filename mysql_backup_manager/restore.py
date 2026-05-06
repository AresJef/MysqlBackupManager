"""Restore service."""

from __future__ import annotations

import logging

from mysql_backup_manager.backup import elapsed_seconds, utc_now
from mysql_backup_manager.compression import open_sql_input
from mysql_backup_manager.config import MySQLConnectionConfig, RestoreConfig
from mysql_backup_manager.exceptions import MySQLCommandError
from mysql_backup_manager.logging import get_logger
from mysql_backup_manager.models import RestoreResult
from mysql_backup_manager.process import build_env, run_command_with_input


class RestoreService:
    """Build and execute mysql restore commands."""

    def __init__(
        self,
        connection: MySQLConnectionConfig,
        config: RestoreConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.connection = connection
        self.config = config
        self.logger = logger or get_logger(__name__)

    def build_command(self) -> list[str]:
        """Build a sanitized mysql command without secrets."""

        command = [
            self.config.mysql_path,
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
        if self.config.force:
            command.append("--force")
        command.extend(self.config.extra_options)
        if self.config.database:
            command.append(self.config.database)
        return command

    async def restore(self) -> RestoreResult:
        """Restore a SQL file using mysql."""

        started_at = utc_now()
        command = self.build_command()
        stderr: str | None = None
        error: str | None = None
        success = False
        try:
            self.logger.info("Starting restore from %s", self.config.input_file)
            with open_sql_input(self.config.input_file, decompress=self.config.decompress) as input_stream:
                stderr = await run_command_with_input(
                    command,
                    input_stream,
                    env=build_env(self.connection.password_value()),
                    timeout=self.config.command_timeout,
                )
            success = True
            self.logger.info("Restore succeeded from %s", self.config.input_file)
        except MySQLCommandError as exc:
            stderr = exc.stderr
            error = str(exc)
            self.logger.exception("Restore failed from %s", self.config.input_file)
        except Exception as exc:
            error = str(exc)
            self.logger.exception("Restore failed from %s", self.config.input_file)

        finished_at = utc_now()
        return RestoreResult(
            success=success,
            input_file=self.config.input_file,
            database=self.config.database,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds(started_at, finished_at),
            command=command,
            stderr=stderr,
            error=error,
        )

