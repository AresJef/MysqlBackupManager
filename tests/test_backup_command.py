from __future__ import annotations

from pathlib import Path

from mysql_backup_manager.backup import BackupService
from mysql_backup_manager.config import DumpConfig, MySQLConnectionConfig


def test_mysqldump_command_contains_expected_options_without_password(tmp_path: Path) -> None:
    service = BackupService(
        MySQLConnectionConfig(host="db", port=3307, user="root", password="secret"),
        DumpConfig(databases=["app"], output_dir=tmp_path),
    )

    command = service.build_command("app")

    assert command[0] == "mysqldump"
    assert "--host=db" in command
    assert "--port=3307" in command
    assert "--user=root" in command
    assert "--single-transaction" in command
    assert "--routines" in command
    assert "--triggers" in command
    assert "--events" in command
    assert "--add-drop-table" in command
    assert "--default-character-set=utf8mb4" in command
    assert command[-1] == "app"
    assert "secret" not in " ".join(command)
    assert not any(arg.startswith("--password") for arg in command)


def test_mysqldump_command_supports_socket_ignore_tables_and_extra_options(tmp_path: Path) -> None:
    service = BackupService(
        MySQLConnectionConfig(user="root", socket="/tmp/mysql.sock"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            ignore_tables=["app.audit_log", "app.sessions"],
            extra_options=["--hex-blob"],
        ),
    )

    command = service.build_command("app")

    assert "--socket=/tmp/mysql.sock" in command
    assert "--ignore-table=app.audit_log" in command
    assert "--ignore-table=app.sessions" in command
    assert command[-2:] == ["--hex-blob", "app"]


def test_output_filename_generation(tmp_path: Path) -> None:
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=["app"], output_dir=tmp_path, filename_template="{database}_{timestamp}.sql"),
    )

    output = service.build_output_path("app")

    assert output.parent == tmp_path
    assert output.name.startswith("app_")
    assert output.suffix == ".sql"




def test_output_filename_rejects_path_traversal(tmp_path: Path) -> None:
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            filename_template="../{database}_{timestamp}.sql",
        ),
    )

    import pytest
    from mysql_backup_manager.exceptions import MySQLBackupError

    with pytest.raises(MySQLBackupError):
        service.build_output_path("app")
