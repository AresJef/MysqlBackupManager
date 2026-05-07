"""Async subprocess execution for native MySQL client tools.

This module is the only place that starts ``mysqldump`` or ``mysql`` processes.
It always uses ``asyncio.create_subprocess_exec`` with argument lists instead of
``shell=True``, streams large inputs/outputs instead of buffering them in memory,
and returns stderr for structured result models.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from mysql_backup_manager.exceptions import (
    MySQLClientNotFoundError,
    MySQLCommandError,
    MySQLDumpNotFoundError,
)


def build_env(password: str | None = None) -> dict[str, str]:
    """Return a subprocess environment containing ``MYSQL_PWD`` when needed.

    :param password: Optional MySQL password. Pass ``None`` when authentication is handled by socket, option files, or existing environment settings.
    :return: A copy of ``os.environ`` with ``MYSQL_PWD`` set only when ``password`` is provided.

    ## Example:
    ```python
    env = build_env("secret")
    env["MYSQL_PWD"]
    # 'secret'
    ```
    """

    env = os.environ.copy()
    if password:
        env["MYSQL_PWD"] = password
    return env


async def _stop_process(process: asyncio.subprocess.Process, *tasks: asyncio.Task[object]) -> None:
    """Terminate a subprocess and cancel helper tasks during error handling.

    :param process: Subprocess returned by ``asyncio.create_subprocess_exec``.
    :param tasks: Helper tasks that should be cancelled after the process is stopped.
    :return: None. The process is waited on and tasks are gathered with exceptions captured.
    """

    if process.returncode is None:
        process.kill()
    with suppress(Exception):
        await process.wait()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _finish_after_stdin_error(
    process: asyncio.subprocess.Process,
    feed_task: asyncio.Task[object],
    stderr_task: asyncio.Task[bytes],
) -> bytes:
    """Collect useful stderr when ``mysql`` closes stdin early.

    :param process: Running or recently exited mysql subprocess.
    :param feed_task: Task that was streaming SQL into process stdin.
    :param stderr_task: Task reading process stderr.
    :return: Captured stderr bytes when available, otherwise ``b""``.
    """

    if not feed_task.done():
        feed_task.cancel()
    await asyncio.gather(feed_task, return_exceptions=True)

    if process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=1.0)
    if process.returncode is None:
        process.kill()
        with suppress(Exception):
            await process.wait()

    if not stderr_task.done():
        with suppress(TimeoutError):
            await asyncio.wait_for(stderr_task, timeout=1.0)
    if stderr_task.done() and not stderr_task.cancelled():
        result = stderr_task.result()
        return result if isinstance(result, bytes) else b""

    stderr_task.cancel()
    await asyncio.gather(stderr_task, return_exceptions=True)
    return b""


async def run_command_capture(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    not_found: type[Exception] = MySQLClientNotFoundError,
) -> tuple[str, str]:
    """Run ``command`` and capture stdout and stderr as text.

    :param command: Argument vector for a native executable, usually ``mysql``.
    :param env: Optional subprocess environment, typically from ``build_env``.
    :param timeout: Optional maximum runtime in seconds.
    :param not_found: Exception type raised when ``command[0]`` cannot be executed.
    :return: Tuple of ``(stdout, stderr)`` decoded with replacement for invalid bytes.
    :raises MySQLClientNotFoundError: By default, when the executable is missing.
    :raises MySQLCommandError: If the process times out or exits with a non-zero status.

    ## Example:
    ```python
    # stdout, stderr = await run_command_capture(["mysql", "--version"])
    ```
    """

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise not_found(f"Executable not found: {command[0]}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        await _stop_process(process)
        raise MySQLCommandError("Command timed out", stderr=None) from exc
    except asyncio.CancelledError:
        await _stop_process(process)
        raise

    stdout = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    if process.returncode != 0:
        raise MySQLCommandError(
            f"Command failed with exit code {process.returncode}",
            returncode=process.returncode,
            stderr=stderr,
        )
    return stdout, stderr


async def run_command_to_file(
    command: list[str],
    output_file: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    not_found: type[Exception] = MySQLDumpNotFoundError,
) -> str:
    """Run ``command`` and stream stdout directly into ``output_file``.

    :param command: Argument vector for a native executable, usually ``mysqldump``.
    :param output_file: File that receives stdout bytes.
    :param env: Optional subprocess environment, typically from ``build_env``.
    :param timeout: Optional maximum runtime in seconds.
    :param not_found: Exception type raised when ``command[0]`` cannot be executed.
    :return: Captured stderr decoded as text. MySQL clients often write warnings to stderr even when the command succeeds.
    :raises MySQLDumpNotFoundError: By default, when the executable is missing.
    :raises MySQLCommandError: If the process times out or exits with a non-zero status.

    ## Example:
    ```python
    # stderr = await run_command_to_file(["mysqldump", "--help"], Path("out.txt"))
    ```
    """

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise not_found(f"Executable not found: {command[0]}") from exc

    async def copy_stdout() -> None:
        """Copy subprocess stdout to the target file in streaming chunks.

        :return: None after stdout reaches EOF.
        """

        assert process.stdout is not None
        with output_file.open("wb") as target:
            while chunk := await process.stdout.read(1024 * 1024):
                await asyncio.to_thread(target.write, chunk)

    assert process.stderr is not None
    copy_task = asyncio.create_task(copy_stdout())
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        await asyncio.wait_for(asyncio.gather(process.wait(), copy_task, stderr_task), timeout=timeout)
        stderr_bytes = stderr_task.result()
    except TimeoutError as exc:
        await _stop_process(process, copy_task, stderr_task)
        raise MySQLCommandError("Command timed out", stderr=None) from exc
    except asyncio.CancelledError:
        await _stop_process(process, copy_task, stderr_task)
        raise
    except Exception:
        await _stop_process(process, copy_task, stderr_task)
        raise

    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    if process.returncode != 0:
        raise MySQLCommandError(
            f"Command failed with exit code {process.returncode}",
            returncode=process.returncode,
            stderr=stderr,
        )
    return stderr


async def run_command_with_input(
    command: list[str],
    input_stream: BinaryIO,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    not_found: type[Exception] = MySQLClientNotFoundError,
) -> str:
    """Run ``command`` and stream ``input_stream`` into stdin.

    :param command: Argument vector for a native executable, usually ``mysql``.
    :param input_stream: Binary stream that yields SQL bytes via ``read``.
    :param env: Optional subprocess environment, typically from ``build_env``.
    :param timeout: Optional maximum runtime in seconds.
    :param not_found: Exception type raised when ``command[0]`` cannot be executed.
    :return: Captured stderr decoded as text.
    :raises MySQLClientNotFoundError: By default, when the executable is missing.
    :raises MySQLCommandError: If the process times out, exits non-zero, or closes stdin before the SQL stream has been fully written.

    ## Example:
    ```python
    # with Path("backup.sql").open("rb") as stream:
    #     stderr = await run_command_with_input(["mysql", "app"], stream)
    ```
    """

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise not_found(f"Executable not found: {command[0]}") from exc

    async def feed_stdin() -> None:
        """Feed SQL bytes into subprocess stdin until the stream is exhausted.

        :return: None after all input bytes are written and stdin is closed.
        """

        assert process.stdin is not None
        while chunk := await asyncio.to_thread(input_stream.read, 1024 * 1024):
            process.stdin.write(chunk)
            await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

    assert process.stderr is not None
    feed_task = asyncio.create_task(feed_stdin())
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        await asyncio.wait_for(asyncio.gather(process.wait(), feed_task, stderr_task), timeout=timeout)
        stderr_bytes = stderr_task.result()
    except TimeoutError as exc:
        await _stop_process(process, feed_task, stderr_task)
        raise MySQLCommandError("Command timed out", stderr=None) from exc
    except (BrokenPipeError, ConnectionResetError) as exc:
        stderr_bytes = await _finish_after_stdin_error(process, feed_task, stderr_task)
        stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
        detail = stderr.strip().splitlines()[-1] if stderr.strip() else "mysql closed stdin before all restore input was written"
        raise MySQLCommandError(
            f"Command failed while streaming restore input: {detail}",
            returncode=process.returncode,
            stderr=stderr,
        ) from exc
    except asyncio.CancelledError:
        await _stop_process(process, feed_task, stderr_task)
        raise
    except Exception:
        await _stop_process(process, feed_task, stderr_task)
        raise

    stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
    if process.returncode != 0:
        raise MySQLCommandError(
            f"Command failed with exit code {process.returncode}",
            returncode=process.returncode,
            stderr=stderr,
        )
    return stderr
