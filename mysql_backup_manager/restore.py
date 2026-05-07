"""Restore service and restore stream helpers.

The restore path uses the native ``mysql`` client and streams SQL bytes into its
stdin. This module also contains small stream wrappers used to prepend database
creation statements and optionally filter ``@@GLOBAL.GTID_PURGED`` statements
for non-replication restores.
"""

from __future__ import annotations

import logging
from typing import BinaryIO

from mysql_backup_manager.utils import elapsed_seconds, utc_now
from mysql_backup_manager.compression import open_sql_input
from mysql_backup_manager.config import MySQLConnectionConfig, RestoreConfig
from mysql_backup_manager.exceptions import MySQLCommandError
from mysql_backup_manager.logging import get_logger
from mysql_backup_manager.models import RestoreResult
from mysql_backup_manager.process import build_env, run_command_with_input


def quote_mysql_identifier(identifier: str) -> str:
    """Quote a MySQL identifier with backticks.

    :param identifier: Raw database, table, or column identifier.
    :return: The identifier surrounded by backticks with embedded backticks escaped by doubling them.

    ## Example:
    ```python
    quote_mysql_identifier("app`db")
    # '`app``db`'
    ```
    """

    return f"`{identifier.replace('`', '``')}`"


def is_gtid_purged_statement(line: bytes) -> bool:
    """Return whether a dump line mutates ``@@GLOBAL.GTID_PURGED``.

    :param line: Raw SQL line read from a binary restore stream.
    :return: ``True`` when the line appears to be a MySQL dump statement that sets ``@@GLOBAL.GTID_PURGED``; otherwise ``False``.

    ## Example:
    ```python
    is_gtid_purged_statement(b"SET @@GLOBAL.GTID_PURGED='uuid:1';\n")
    # True
    ```
    """

    normalized = line.lstrip().upper()
    return b"@@GLOBAL.GTID_PURGED" in normalized and (
        normalized.startswith(b"SET ")
        or normalized.startswith(b"/*!")
        or normalized.startswith(b"-- SET ")
    )


class GtidPurgedFilterStream:
    """Read-only binary stream that removes GTID purge statements.

    :param stream: Binary SQL stream opened for reading.
    :return: A stream-like object with a ``read`` method. It yields the original SQL bytes except lines that set ``@@GLOBAL.GTID_PURGED``.

    ## Example:
    ```python
    import io

    stream = GtidPurgedFilterStream(
        io.BytesIO(b"SET @@GLOBAL.GTID_PURGED='x';\nSELECT 1;\n")
    )
    stream.read()
    # b'SELECT 1;\n'
    ```
    """

    def __init__(self, stream: BinaryIO) -> None:
        """Wrap an existing binary stream without taking ownership of it.

        :param stream: Binary stream that supports ``readline`` and returns SQL bytes.
        :return: None. The wrapped stream remains owned by the caller and should be closed by the caller.
        """

        self._stream = stream
        self._buffer = bytearray()

    def _read_filtered_line(self) -> bytes:
        """Read the next line that is not a GTID purge statement.

        :return: The next non-GTID line as bytes, or ``b""`` at end of stream.
        """

        while True:
            line = self._stream.readline()
            if not line:
                return b""
            if not is_gtid_purged_statement(line):
                return line

    def read(self, size: int = -1) -> bytes:
        """Read filtered bytes, honoring normal ``read`` size semantics.

        :param size: Maximum number of bytes to return. Use ``-1`` to read until EOF.
        :return: Bytes from the wrapped stream after dropping GTID purge statements.
        """

        if size == 0:
            return b""
        if size is None or size < 0:
            chunks: list[bytes] = []
            if self._buffer:
                chunks.append(bytes(self._buffer))
                self._buffer.clear()
            while line := self._read_filtered_line():
                chunks.append(line)
            return b"".join(chunks)

        while len(self._buffer) < size:
            line = self._read_filtered_line()
            if not line:
                break
            self._buffer.extend(line)
        output = bytes(self._buffer[:size])
        del self._buffer[:size]
        return output


class PrefixedBinaryStream:
    """Read-only stream that emits a byte prefix before another stream.

    :param prefix: Bytes to emit before the wrapped stream.
    :param stream: Binary stream to read after the prefix has been exhausted.
    :return: A stream-like object with a ``read`` method.

    ## Example:
    ```python
    import io

    stream = PrefixedBinaryStream(
        b"USE `app`;\n",
        io.BytesIO(b"SELECT 1;\n"),
    )
    stream.read()
    # b'USE `app`;\nSELECT 1;\n'
    ```
    """

    def __init__(self, prefix: bytes, stream: BinaryIO) -> None:
        """Create a stream that reads ``prefix`` before ``stream``.

        :param prefix: Bytes to return before reading from ``stream``.
        :param stream: Binary stream returned after the prefix has been consumed.
        :return: None.
        """

        self._prefix = memoryview(prefix)
        self._position = 0
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        """Read bytes from the prefix first, then from the wrapped stream.

        :param size: Maximum number of bytes to return. Use ``-1`` to read until EOF.
        :return: Bytes from the prefix and wrapped stream in order.
        """

        if size == 0:
            return b""
        remaining_prefix = len(self._prefix) - self._position
        if remaining_prefix <= 0:
            return self._stream.read(size)
        if size is None or size < 0:
            prefix = self._prefix[self._position :].tobytes()
            self._position = len(self._prefix)
            return prefix + self._stream.read()
        prefix_size = min(size, remaining_prefix)
        prefix = self._prefix[self._position : self._position + prefix_size].tobytes()
        self._position += prefix_size
        if prefix_size == size:
            return prefix
        return prefix + self._stream.read(size - prefix_size)


class RestoreService:
    """Service responsible for building and running ``mysql`` restores.

    :param connection: MySQL connection settings used to build client commands and pass the password through ``MYSQL_PWD``.
    :param config: Restore settings describing the input file, database behavior, stream filters, extra options, and timeout.
    :param logger: Optional logger for restore lifecycle messages.
    :return: A ``RestoreService`` instance.

    ## Example:
    ```python
    from pathlib import Path
    path = Path("example.sql")
    path.write_text("SELECT 1;", encoding="utf-8")
    # 9
    service = RestoreService(MySQLConnectionConfig(user="root"), RestoreConfig(database="app", input_file=path))
    service.build_command()[-1]
    # 'app'
    path.unlink()
    ```
    """

    def __init__(
        self,
        connection: MySQLConnectionConfig,
        config: RestoreConfig,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a restore service for a connection and restore configuration.

        :param connection: Validated connection settings for the target MySQL server.
        :param config: Validated restore configuration for one input file.
        :param logger: Optional logger. When omitted, the package logger is used.
        :return: None. The constructor stores dependencies only and does not start a process.
        """

        self.connection = connection
        self.config = config
        self.logger = logger or get_logger(__name__)

    def build_command(self) -> list[str]:
        """Build the ``mysql`` argument vector for restore.

        :return: A sanitized command list suitable for ``asyncio.create_subprocess_exec``. The password is never included. The target database is appended only when it is configured and ``create_database_if_missing`` is false.

        ## Example:
        ```python
        from pathlib import Path
        path = Path("example.sql")
        path.write_text("SELECT 1;", encoding="utf-8")
        # 9
        service = RestoreService(MySQLConnectionConfig(user="root"), RestoreConfig(database="app", input_file=path))
        service.build_command()[-1]
        # 'app'
        path.unlink()
        ```
        """

        command = [
            self.config.mysql_path,
            f"--host={self.connection.host}",
            f"--port={self.connection.port}",
            f"--user={self.connection.user}",
        ]
        if self.connection.socket:
            command.append(f"--socket={self.connection.socket}")
        if self.connection.default_character_set:
            command.append(
                f"--default-character-set={self.connection.default_character_set}"
            )
        if self.connection.connect_timeout is not None:
            command.append(f"--connect-timeout={self.connection.connect_timeout}")
        if self.config.force:
            command.append("--force")
        command.extend(self.config.extra_options)
        if self.config.database and not self.config.create_database_if_missing:
            command.append(self.config.database)
        return command

    def _restore_input_stream(self, input_stream: BinaryIO) -> BinaryIO:
        """Compose restore stream filters and prefixes for the current config.

        :param input_stream: Open binary SQL stream, possibly already decompressed by ``open_sql_input``.
        :return: A binary stream object that may strip GTID purge statements, prepend database creation/use SQL, or simply proxy the original stream.
        """

        stream: BinaryIO = input_stream
        if self.config.strip_gtid_purged:
            stream = GtidPurgedFilterStream(stream)
        if not self.config.database or not self.config.create_database_if_missing:
            return stream
        database = quote_mysql_identifier(self.config.database)
        prefix = f"CREATE DATABASE IF NOT EXISTS {database};\nUSE {database};\n".encode(
            "utf-8"
        )
        return PrefixedBinaryStream(prefix, stream)

    async def restore(self) -> RestoreResult:
        """Stream the configured SQL file into ``mysql`` and return a result.

        :return: ``RestoreResult`` containing success status, input path, target database, elapsed time, sanitized command, stderr, and error details.

        ## Example:
        ```python
        # result = await service.restore()
        # if not result.success: raise RuntimeError(result.error)
        ```
        """

        started_at = utc_now()
        command = self.build_command()
        stderr: str | None = None
        error: str | None = None
        success = False
        try:
            self.logger.info("Starting restore from %s", self.config.input_file)
            with open_sql_input(
                self.config.input_file, decompress=self.config.decompress
            ) as input_stream:
                stderr = await run_command_with_input(
                    command,
                    self._restore_input_stream(input_stream),
                    env=build_env(self.connection.password_value()),
                    timeout=self.config.command_timeout,
                )
            success = True
            self.logger.info("Restore succeeded from %s", self.config.input_file)
        except MySQLCommandError as exc:
            stderr = exc.stderr
            error = str(exc)
            self.logger.error(
                "Restore failed from %s: %s%s",
                self.config.input_file,
                error,
                f"; stderr: {stderr.strip()}" if stderr and stderr.strip() else "",
            )
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
