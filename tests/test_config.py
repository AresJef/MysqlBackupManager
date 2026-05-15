from __future__ import annotations

from pathlib import Path

import pytest

from mysql_backup_manager.config import DumpConfig, MySQLConnectionConfig, RestoreConfig, ScheduleConfig
from mysql_backup_manager.exceptions import BackupConfigError, RestoreConfigError


def test_connection_password_is_hidden_and_loaded_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_PWD", "secret")
    config = MySQLConnectionConfig(user="root")

    assert config.password_value() == "secret"
    assert "secret" not in repr(config)


def test_dump_config_requires_databases(tmp_path: Path) -> None:
    with pytest.raises(BackupConfigError):
        DumpConfig(databases=[], output_dir=tmp_path)


def test_dump_config_creates_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / "nested" / "backups"

    config = DumpConfig(databases=["app"], output_dir=output_dir)

    assert config.output_dir.exists()


def test_filename_template_requires_database_and_timestamp(tmp_path: Path) -> None:
    with pytest.raises(BackupConfigError):
        DumpConfig(databases=["app"], output_dir=tmp_path, filename_template="{database}.sql")


def test_ignore_tables_must_be_db_dot_table(tmp_path: Path) -> None:
    with pytest.raises(BackupConfigError):
        DumpConfig(databases=["app"], output_dir=tmp_path, ignore_tables=["users"])


def test_restore_config_accepts_sql_and_sql_gz(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql_gz = tmp_path / "backup.sql.gz"
    sql.write_text("select 1;", encoding="utf-8")
    sql_gz.write_bytes(b"not really gzip for validation only")

    assert RestoreConfig(input_file=sql).input_file == sql
    assert RestoreConfig(input_file=sql_gz).input_file == sql_gz


def test_restore_config_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RestoreConfigError):
        RestoreConfig(input_file=tmp_path / "missing.sql")


def test_schedule_config_validation() -> None:
    with pytest.raises(ValueError):
        ScheduleConfig(enabled=True)
    with pytest.raises(ValueError):
        ScheduleConfig(enabled=True, cron="0 3 * * *", interval_seconds=60)




def test_dump_config_rejects_empty_mysqldump_path(tmp_path: Path) -> None:
    with pytest.raises(BackupConfigError):
        DumpConfig(databases=["app"], output_dir=tmp_path, mysqldump_path="   ")


def test_restore_config_rejects_empty_mysql_path(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")
    with pytest.raises(RestoreConfigError):
        RestoreConfig(input_file=sql, mysql_path="   ")



def test_schedule_config_rejects_invalid_cron_and_timezone() -> None:
    with pytest.raises(ValueError):
        ScheduleConfig(enabled=True, cron="not cron")
    with pytest.raises(ValueError):
        ScheduleConfig(enabled=True, interval_seconds=60, timezone="No/SuchZone")


def test_config_normalizes_connection_and_database_names(tmp_path: Path) -> None:
    connection = MySQLConnectionConfig(host=" db ", user=" root ", socket=" /tmp/mysql.sock ")
    dump = DumpConfig(databases=[" app "], output_dir=tmp_path)
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")
    restore = RestoreConfig(database=" app ", input_file=sql)

    assert connection.host == "db"
    assert connection.user == "root"
    assert connection.socket == "/tmp/mysql.sock"
    assert dump.databases == ["app"]
    assert restore.database == "app"


@pytest.mark.parametrize(
    "option",
    [
        "--password",
        "--password=secret",
        "--password secret",
        "--password\tsecret",
        "--password\nsecret",
        "-p",
        "-psecret",
    ],
)
def test_extra_options_reject_password_arguments(tmp_path: Path, option: str) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")

    with pytest.raises(ValueError):
        DumpConfig(databases=["app"], output_dir=tmp_path, extra_options=[option])
    with pytest.raises(ValueError):
        RestoreConfig(input_file=sql, extra_options=[option])


def test_retention_match_pattern_must_stay_inside_output_dir() -> None:
    from mysql_backup_manager.config import RetentionConfig

    with pytest.raises(ValueError):
        RetentionConfig(match_pattern="../*.sql")


def test_retention_config_omitted_limits_are_disabled() -> None:
    from mysql_backup_manager.config import RetentionConfig

    config = RetentionConfig(keep_days=7)

    assert config.keep_last is None
    assert config.keep_days == 7


def test_retention_config_defaults_do_not_delete_by_count_or_age() -> None:
    from mysql_backup_manager.config import RetentionConfig

    config = RetentionConfig()

    assert config.keep_last is None
    assert config.keep_days is None


def test_output_dir_expands_user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    config = DumpConfig(databases=["app"], output_dir=Path("~/backups"))

    assert config.output_dir == tmp_path / "backups"
    assert config.output_dir.exists()


def test_restore_input_file_expands_user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")

    config = RestoreConfig(input_file=Path("~/backup.sql"))

    assert config.input_file == sql


def test_restore_database_rejects_null_byte(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")

    with pytest.raises(RestoreConfigError):
        RestoreConfig(database="bad\x00name", input_file=sql)


def test_restore_create_database_requires_database(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")

    with pytest.raises(RestoreConfigError):
        RestoreConfig(input_file=sql, create_database_if_missing=True)


def test_filename_template_rejects_complex_field_access(tmp_path: Path) -> None:
    with pytest.raises(BackupConfigError):
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            filename_template="{database.__class__}_{timestamp}.sql",
        )


def test_restore_config_rejects_directory_input_file(tmp_path: Path) -> None:
    directory_named_sql = tmp_path / "backup.sql"
    directory_named_sql.mkdir()

    with pytest.raises(RestoreConfigError):
        RestoreConfig(input_file=directory_named_sql)


def test_extra_options_are_stripped_and_blank_options_rejected(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")

    dump = DumpConfig(databases=["app"], output_dir=tmp_path, extra_options=[" --quick "])
    restore = RestoreConfig(input_file=sql, extra_options=[" --binary-mode "])

    assert dump.extra_options == ["--quick"]
    assert restore.extra_options == ["--binary-mode"]
    with pytest.raises(ValueError):
        DumpConfig(databases=["app"], output_dir=tmp_path, extra_options=["   "])


def test_executable_paths_are_stripped(tmp_path: Path) -> None:
    sql = tmp_path / "backup.sql"
    sql.write_text("select 1;", encoding="utf-8")

    dump = DumpConfig(
        databases=["app"],
        output_dir=tmp_path,
        mysqldump_path=" mysqldump ",
        mysql_path=" /usr/local/bin/mysql ",
    )
    restore = RestoreConfig(input_file=sql, mysql_path=" mysql ")

    assert dump.mysqldump_path == "mysqldump"
    assert dump.mysql_path == "/usr/local/bin/mysql"
    assert dump.validate_database_exists is True
    assert dump.validate_database_has_objects is True
    assert dump.validate_dump_content is True
    assert restore.mysql_path == "mysql"


def test_dump_config_normalizes_and_validates_gtid_purged(tmp_path: Path) -> None:
    config = DumpConfig(databases=["app"], output_dir=tmp_path, set_gtid_purged="auto")

    assert config.set_gtid_purged == "AUTO"

    with pytest.raises(BackupConfigError):
        DumpConfig(databases=["app"], output_dir=tmp_path, set_gtid_purged="INVALID")


def test_dump_config_can_disable_stale_temp_cleanup(tmp_path: Path) -> None:
    config = DumpConfig(
        databases=["app"],
        output_dir=tmp_path,
        cleanup_stale_temp_files=False,
        stale_temp_file_age_seconds=None,
    )

    assert config.cleanup_stale_temp_files is False
    assert config.stale_temp_file_age_seconds is None


def test_dump_config_validates_temp_dir(tmp_path: Path) -> None:
    temp_dir = tmp_path / "manager-temp"
    config = DumpConfig(databases=["app"], output_dir=tmp_path / "backups", temp_dir=temp_dir)

    assert config.temp_dir == temp_dir
    assert not temp_dir.exists()

    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(BackupConfigError):
        DumpConfig(databases=["app"], output_dir=tmp_path / "other-backups", temp_dir=file_path)
