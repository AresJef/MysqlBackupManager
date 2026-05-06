from __future__ import annotations

from pathlib import Path

from mysql_backup_manager.config import MySQLConnectionConfig, RestoreConfig
from mysql_backup_manager.restore import RestoreService


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

    monkeypatch.setattr("mysql_backup_manager.restore.run_command_with_input", fake_run)
    service = RestoreService(
        MySQLConnectionConfig(user="root"),
        RestoreConfig(input_file=sql, command_timeout=9),
    )

    result = await service.restore()

    assert result.success is True
    assert seen_timeout == 9
