"""Async subprocess execution for native MySQL client tools."""

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
    """Build a subprocess environment with MYSQL_PWD when needed."""

    env = os.environ.copy()
    if password:
        env["MYSQL_PWD"] = password
    return env


async def _stop_process(process: asyncio.subprocess.Process, *tasks: asyncio.Task[object]) -> None:
    if process.returncode is None:
        process.kill()
    with suppress(Exception):
        await process.wait()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_command_to_file(
    command: list[str],
    output_file: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    not_found: type[Exception] = MySQLDumpNotFoundError,
) -> str:
    """Run a command and stream stdout directly to a file."""

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
        assert process.stdout is not None
        with output_file.open("wb") as target:
            while chunk := await process.stdout.read(1024 * 1024):
                target.write(chunk)

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
    """Run a command and stream bytes into stdin."""

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
        assert process.stdin is not None
        while chunk := input_stream.read(1024 * 1024):
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
        await _stop_process(process, feed_task, stderr_task)
        stderr_bytes = stderr_task.result() if stderr_task.done() and not stderr_task.cancelled() else b""
        stderr = stderr_bytes.decode(errors="replace") if stderr_bytes else ""
        raise MySQLCommandError(
            "Command closed stdin before restore input was fully written",
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
