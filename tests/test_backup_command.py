from __future__ import annotations

from pathlib import Path

from mysql_backup_manager.backup import BackupService, dump_file_contains_database_objects
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
    assert "--connect-timeout=10" not in command
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


def test_database_exists_command_uses_mysql_without_password(tmp_path: Path) -> None:
    service = BackupService(
        MySQLConnectionConfig(host="db", port=3307, user="root", password="secret"),
        DumpConfig(databases=["app"], output_dir=tmp_path, mysql_path="/usr/bin/mysql"),
    )

    command = service.build_database_exists_command("app's db")

    assert command[0] == "/usr/bin/mysql"
    assert "--host=db" in command
    assert "--port=3307" in command
    assert "--user=root" in command
    assert "--batch" in command
    assert "--skip-column-names" in command
    assert "--connect-timeout=10" in command
    assert "secret" not in " ".join(command)
    assert command[-1] == "--execute=SELECT EXISTS(SELECT 1 FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = 'app''s db')"

def test_database_object_count_command_uses_mysql_without_password(tmp_path: Path) -> None:
    service = BackupService(
        MySQLConnectionConfig(host="db", port=3307, user="root", password="secret"),
        DumpConfig(databases=["app"], output_dir=tmp_path, mysql_path="/usr/bin/mysql"),
    )

    command = service.build_database_object_count_command("app")

    assert command[0] == "/usr/bin/mysql"
    assert "secret" not in " ".join(command)
    assert command[-1] == "--execute=SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'app'"

def test_dump_file_contains_database_objects_detects_schema_and_data(tmp_path: Path) -> None:
    schema_dump = tmp_path / "schema.sql"
    schema_dump.write_text("CREATE TABLE `listing` (`id` int);", encoding="utf-8")
    data_dump = tmp_path / "data.sql"
    data_dump.write_text("INSERT INTO `listing` VALUES (1);", encoding="utf-8")
    empty_dump = tmp_path / "empty.sql"
    empty_dump.write_text("-- MySQL dump header only", encoding="utf-8")

    assert dump_file_contains_database_objects(schema_dump) is True
    assert dump_file_contains_database_objects(data_dump) is True
    assert dump_file_contains_database_objects(empty_dump) is False
