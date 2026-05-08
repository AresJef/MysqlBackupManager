from __future__ import annotations

import importlib
from pathlib import Path

from mysql_backup_manager.config import MySQLConnectionConfig, RestoreConfig
from mysql_backup_manager.restore import RestoreService


restore_module = importlib.import_module("mysql_backup_manager.restore")


def test_mysql_restore_command_with_database_without_password(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")
    service = RestoreService(
        MySQLConnectionConfig(host="db", port=3307, user="root", password="secret"),
        RestoreConfig(database="app", input_file=sql, force=True),
    )

    command = service.build_command()

    assert command[0] == "mysql"
    assert "--host=db" in command
    assert "--port=3307" in command
    assert "--user=root" in command
    assert "--force" in command
    assert command[-1] == "app"
    assert "secret" not in " ".join(command)
    assert not any(arg.startswith("--password") for arg in command)


def test_mysql_restore_command_without_database_allows_sql_to_choose_db(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("create database app; use app;", encoding="utf-8")
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(input_file=sql, extra_options=["--binary-mode"]),
    )

    command = service.build_command()

    assert command[-1] == "--binary-mode"
    assert "app" not in command




async def test_restore_passes_command_timeout(monkeypatch, tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")
    seen_timeout = None

    async def fake_run(command, input_stream, **kwargs):
        nonlocal seen_timeout
        seen_timeout = kwargs["timeout"]
        assert input_stream.read() == b"select 1;"
        return ""

    monkeypatch.setattr(restore_module, "run_command_with_input", fake_run)
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(input_file=sql, command_timeout=9),
    )

    result = await service.restore()

    assert result.success is True
    assert seen_timeout == 9


def test_mysql_restore_create_database_omits_database_argument(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(database="app", input_file=sql, create_database_if_missing=True),
    )

    command = service.build_command()

    assert command[-1] != "app"
    assert "app" not in command


def test_restore_create_database_prefixes_sql_stream(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("CREATE TABLE t(id int);", encoding="utf-8")
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(database="app`copy", input_file=sql, create_database_if_missing=True),
    )

    with sql.open("rb") as input_stream:
        prefixed = service._restore_input_stream(input_stream)
        content = prefixed.read().decode("utf-8")

    assert content.startswith("CREATE DATABASE IF NOT EXISTS `app``copy`;\nUSE `app``copy`;\n")
    assert content.endswith("CREATE TABLE t(id int);")


async def test_restore_create_database_streams_prefixed_input(monkeypatch, tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("CREATE TABLE t(id int);", encoding="utf-8")
    seen_command = None
    seen_payload = None

    async def fake_run(command, input_stream, **kwargs):
        nonlocal seen_command, seen_payload
        seen_command = command
        seen_payload = input_stream.read().decode("utf-8")
        return ""

    monkeypatch.setattr(restore_module, "run_command_with_input", fake_run)
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(database="app_copy", input_file=sql, create_database_if_missing=True),
    )

    result = await service.restore()

    assert result.success is True
    assert seen_command is not None
    assert "app_copy" not in seen_command
    assert seen_payload.startswith("CREATE DATABASE IF NOT EXISTS `app_copy`;\nUSE `app_copy`;\n")


def test_restore_strip_gtid_purged_filters_dump_lines(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text(
        "CREATE TABLE t(id int);\n"
        "SET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ 'uuid:1-2';\n"
        "INSERT INTO t VALUES (1);\n",
        encoding="utf-8",
    )
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(input_file=sql, strip_gtid_purged=True),
    )

    with sql.open("rb") as input_stream:
        content = service._restore_input_stream(input_stream).read().decode("utf-8")

    assert "GTID_PURGED" not in content
    assert "CREATE TABLE t" in content
    assert "INSERT INTO t" in content


def test_restore_strip_gtid_purged_supports_chunked_reads(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text(
        "abc\n"
        "/*!50718 SET @@GLOBAL.GTID_PURGED='uuid:1-2' */;\n"
        "def\n",
        encoding="utf-8",
    )
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(input_file=sql, strip_gtid_purged=True),
    )

    with sql.open("rb") as input_stream:
        stream = service._restore_input_stream(input_stream)
        chunks = []
        while chunk := stream.read(2):
            chunks.append(chunk)

    assert b"".join(chunks) == b"abc\ndef\n"


def test_restore_create_database_and_strip_gtid_purged(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text(
        "SET @@GLOBAL.GTID_PURGED='uuid:1-2';\n"
        "CREATE TABLE t(id int);",
        encoding="utf-8",
    )
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(
            database="app_copy",
            input_file=sql,
            create_database_if_missing=True,
            strip_gtid_purged=True,
        ),
    )

    with sql.open("rb") as input_stream:
        content = service._restore_input_stream(input_stream).read().decode("utf-8")

    assert content.startswith("CREATE DATABASE IF NOT EXISTS `app_copy`;\nUSE `app_copy`;\n")
    assert "GTID_PURGED" not in content
    assert content.endswith("CREATE TABLE t(id int);")


def test_restore_strip_gtid_purged_preserves_data_mentions(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text(
        "INSERT INTO logs(message) VALUES ('mentions GTID_PURGED but is data');\n"
        "SET @@GLOBAL.GTID_PURGED='uuid:1-2';\n",
        encoding="utf-8",
    )
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(input_file=sql, strip_gtid_purged=True),
    )

    with sql.open("rb") as input_stream:
        content = service._restore_input_stream(input_stream).read().decode("utf-8")

    assert "mentions GTID_PURGED but is data" in content
    assert "SET @@GLOBAL.GTID_PURGED" not in content
