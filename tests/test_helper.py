from __future__ import annotations

from datetime import datetime, timezone
import importlib
import inspect
from pathlib import Path

from mysql_backup_manager import helper
from mysql_backup_manager.models import BackupResult, RestoreResult


def test_package_exports_primary_helper_functions() -> None:
    import mysql_backup_manager
    from mysql_backup_manager import backup, restore, scheduled_backup

    backup_module = importlib.import_module("mysql_backup_manager.backup")
    restore_module = importlib.import_module("mysql_backup_manager.restore")

    assert mysql_backup_manager.helper is helper
    assert backup is helper.backup
    assert restore is helper.restore
    assert scheduled_backup is helper.scheduled_backup
    assert backup_module.BackupService is mysql_backup_manager.BackupService
    assert restore_module.RestoreService is mysql_backup_manager.RestoreService


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_helper_public_signatures_are_intentionally_simplified() -> None:
    backup_signature = inspect.signature(helper.backup)
    restore_signature = inspect.signature(helper.restore)
    scheduled_signature = inspect.signature(helper.scheduled_backup)

    assert backup_signature.parameters["database"].default is inspect.Signature.empty
    assert scheduled_signature.parameters["database"].default is inspect.Signature.empty
    assert "database" not in restore_signature.parameters
    assert "target_database" in restore_signature.parameters
    assert "input_file" not in restore_signature.parameters
    assert "mysql_path" not in restore_signature.parameters
    assert "create_database_if_missing" in restore_signature.parameters
    assert "include_database_statements" in backup_signature.parameters
    assert "include_database_statements" in scheduled_signature.parameters
    assert "temp_dir" in backup_signature.parameters
    assert "temp_dir" in scheduled_signature.parameters
    assert "hex_blob" in backup_signature.parameters
    assert "hex_blob" in scheduled_signature.parameters
    assert "hex_blob" not in restore_signature.parameters


def test_backup_helper_builds_configurable_single_database_manager(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeManager:
        instances = []

        def __init__(self, connection, dump, retention=None, logger=None) -> None:
            self.connection = connection
            self.dump = dump
            self.retention = retention
            self.logger = logger
            self.__class__.instances.append(self)

        def backup_database_sync(self, database: str) -> BackupResult:
            return BackupResult(
                database=database,
                success=True,
                output_file=tmp_path / f"{database}.sql",
                compressed_file=tmp_path / f"{database}.sql.gz",
                checksum_file=None,
                checksum=None,
                started_at=_now(),
                finished_at=_now(),
                elapsed_seconds=0.0,
                file_size_bytes=None,
                command=[],
                stderr=None,
                error=None,
            )

        def backup_all_sync(self):
            raise AssertionError("single database helper should not call backup_all_sync")

    monkeypatch.setattr(helper, "MySQLBackupManager", FakeManager)

    artifact = helper.backup(
        user="backup_user",
        password="secret",
        host="db.example.com",
        port=3307,
        socket="/tmp/mysql.sock",
        backup_dir=tmp_path,
        database="app",
        temp_dir=tmp_path / "manager-temp",
        compress=True,
        command_timeout=99,
        set_gtid_purged="AUTO",
        ignore_tables=["app.audit_log"],
        include_database_statements=True,
        quick=True,
        hex_blob=True,
        extra_options=["--skip-comments"],
        validate_database_has_objects=False,
    )

    manager = FakeManager.instances[-1]
    assert artifact == tmp_path / "app.sql.gz"
    assert manager.connection.host == "db.example.com"
    assert manager.connection.port == 3307
    assert manager.connection.user == "backup_user"
    assert manager.connection.socket == "/tmp/mysql.sock"
    assert manager.dump.databases == ["app"]
    assert manager.dump.temp_dir == tmp_path / "manager-temp"
    assert manager.dump.compress is True
    assert manager.dump.command_timeout == 99
    assert manager.dump.set_gtid_purged == "AUTO"
    assert manager.dump.ignore_tables == ["app.audit_log"]
    assert manager.dump.extra_options == ["--databases", "--quick", "--hex-blob", "--skip-comments"]
    assert manager.connection.default_character_set == "utf8mb4"
    assert manager.connection.connect_timeout == 10
    assert manager.dump.filename_template == "{database}_{timestamp}.sql"
    assert manager.dump.timestamp_format == "%Y%m%d_%H%M%S"
    assert manager.dump.mysqldump_path == "mysqldump"
    assert manager.dump.compression_format == "gzip"
    assert manager.dump.checksum_algorithm == "sha256"
    assert manager.dump.validate_database_has_objects is False


def test_backup_helper_can_disable_quick_and_enable_hex_blob(monkeypatch, tmp_path: Path) -> None:
    class FakeManager:
        instances = []

        def __init__(self, connection, dump, retention=None, logger=None) -> None:
            self.dump = dump
            self.__class__.instances.append(self)

        def backup_database_sync(self, database: str) -> BackupResult:
            return BackupResult(
                database=database,
                success=True,
                output_file=tmp_path / f"{database}.sql",
                compressed_file=None,
                checksum_file=None,
                checksum=None,
                started_at=_now(),
                finished_at=_now(),
                elapsed_seconds=0.0,
                file_size_bytes=None,
                command=[],
                stderr=None,
                error=None,
            )

        def backup_all_sync(self):
            raise AssertionError("single database helper should not call backup_all_sync")

    monkeypatch.setattr(helper, "MySQLBackupManager", FakeManager)

    helper.backup(
        user="root",
        backup_dir=tmp_path,
        database="app",
        quick=False,
        hex_blob=True,
        extra_options=["--skip-comments"],
    )

    assert FakeManager.instances[-1].dump.extra_options == ["--hex-blob", "--skip-comments"]


def test_backup_helper_normalizes_extra_options_before_managed_flag_checks(
    monkeypatch, tmp_path: Path
) -> None:
    class FakeManager:
        instances = []

        def __init__(self, connection, dump, retention=None, logger=None) -> None:
            self.dump = dump
            self.__class__.instances.append(self)

        def backup_database_sync(self, database: str) -> BackupResult:
            return BackupResult(
                database=database,
                success=True,
                output_file=tmp_path / f"{database}.sql",
                compressed_file=None,
                checksum_file=None,
                checksum=None,
                started_at=_now(),
                finished_at=_now(),
                elapsed_seconds=0.0,
                file_size_bytes=None,
                command=[],
                stderr=None,
                error=None,
            )

        def backup_all_sync(self):
            raise AssertionError("single database helper should not call backup_all_sync")

    monkeypatch.setattr(helper, "MySQLBackupManager", FakeManager)

    helper.backup(
        user="root",
        backup_dir=tmp_path,
        database="app",
        include_database_statements=True,
        quick=True,
        extra_options=[" --databases ", " --quick "],
    )

    assert FakeManager.instances[-1].dump.extra_options == ["--databases", "--quick"]


def test_backup_helper_can_return_multiple_results(monkeypatch, tmp_path: Path) -> None:
    class FakeManager:
        def __init__(self, connection, dump, retention=None, logger=None) -> None:
            self.dump = dump

        def backup_database_sync(self, database: str):
            raise AssertionError("multi database helper should call backup_all_sync")

        def backup_all_sync(self):
            return [
                BackupResult(
                    database=database,
                    success=True,
                    output_file=tmp_path / f"{database}.sql",
                    compressed_file=None,
                    checksum_file=None,
                    checksum=None,
                    started_at=_now(),
                    finished_at=_now(),
                    elapsed_seconds=0.0,
                    file_size_bytes=None,
                    command=[],
                    stderr=None,
                    error=None,
                )
                for database in self.dump.databases
            ]

    monkeypatch.setattr(helper, "MySQLBackupManager", FakeManager)

    results = helper.backup(
        user="root",
        backup_dir=tmp_path,
        database=["app", "analytics"],
        return_results=True,
    )

    assert isinstance(results, list)
    assert [result.database for result in results] == ["app", "analytics"]


def test_backup_helper_requires_results_when_failures_are_not_raised(tmp_path: Path) -> None:
    try:
        helper.backup(
            user="root",
            backup_dir=tmp_path,
            database="app",
            raise_on_failure=False,
        )
    except ValueError as exc:
        assert "return_results=True" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_restore_helper_builds_configurable_restore_service(monkeypatch, tmp_path: Path) -> None:
    backup_file = tmp_path / "app.sql"
    backup_file.write_text("SELECT 1;", encoding="utf-8")

    class FakeRestoreService:
        instances = []

        def __init__(self, connection, config, logger=None) -> None:
            self.connection = connection
            self.config = config
            self.logger = logger
            self.__class__.instances.append(self)

        async def restore(self) -> RestoreResult:
            return RestoreResult(
                success=True,
                input_file=self.config.input_file,
                database=self.config.database,
                started_at=_now(),
                finished_at=_now(),
                elapsed_seconds=0.0,
                command=[],
                stderr=None,
                error=None,
            )

    monkeypatch.setattr(helper, "RestoreService", FakeRestoreService)

    result = helper.restore(
        user="restore_user",
        password="secret",
        host="replica.example.com",
        port=3307,
        backup_file=backup_file,
        target_database="app_copy",
        create_database_if_missing=True,
        strip_gtid_purged=True,
        force=True,
        extra_options=["--binary-mode"],
        command_timeout=120,
        return_result=True,
    )

    service = FakeRestoreService.instances[-1]
    assert isinstance(result, RestoreResult)
    assert service.connection.host == "replica.example.com"
    assert service.connection.user == "restore_user"
    assert service.config.input_file == backup_file
    assert service.config.database == "app_copy"
    assert service.config.create_database_if_missing is True
    assert service.config.strip_gtid_purged is True
    assert service.config.force is True
    assert service.config.extra_options == ["--binary-mode"]
    assert service.config.command_timeout == 120
    assert service.connection.default_character_set == "utf8mb4"
    assert service.connection.connect_timeout == 10
    assert service.config.mysql_path == "mysql"


def test_restore_helper_requires_target_database_when_creating_database(tmp_path: Path) -> None:
    backup_file = tmp_path / "app.sql"
    backup_file.write_text("SELECT 1;", encoding="utf-8")

    try:
        helper.restore(
            user="root",
            backup_file=backup_file,
            create_database_if_missing=True,
        )
    except ValueError as exc:
        assert "target_database" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_restore_helper_adds_no_database_selected_hint(monkeypatch, tmp_path: Path) -> None:
    backup_file = tmp_path / "app.sql"
    backup_file.write_text("CREATE TABLE t(id int);", encoding="utf-8")

    class FakeRestoreService:
        def __init__(self, connection, config, logger=None) -> None:
            self.config = config

        async def restore(self) -> RestoreResult:
            return RestoreResult(
                success=False,
                input_file=self.config.input_file,
                database=self.config.database,
                started_at=_now(),
                finished_at=_now(),
                elapsed_seconds=0.0,
                command=[],
                stderr="ERROR 1046 (3D000): No database selected",
                error="Command failed while streaming restore input",
            )

    monkeypatch.setattr(helper, "RestoreService", FakeRestoreService)

    try:
        helper.restore(user="root", backup_file=backup_file)
    except RuntimeError as exc:
        message = str(exc)
        assert "target_database" in message
        assert "include_database_statements=True" in message
    else:
        raise AssertionError("expected RuntimeError")


def test_scheduled_backup_helper_wires_schedule_and_retention(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeManager:
        instances = []

        def __init__(self, connection, dump, retention=None, logger=None) -> None:
            self.connection = connection
            self.dump = dump
            self.retention = retention
            self.logger = logger
            self.__class__.instances.append(self)

    class FakeScheduler:
        instances = []

        def __init__(self, manager, config, logger=None) -> None:
            self.manager = manager
            self.config = config
            self.logger = logger
            self.stop_on_failure = None
            self.__class__.instances.append(self)

        async def run_forever(self, *, stop_on_failure: bool = False) -> None:
            self.stop_on_failure = stop_on_failure

    monkeypatch.setattr(helper, "MySQLBackupManager", FakeManager)
    monkeypatch.setattr(helper, "SchedulerService", FakeScheduler)

    helper.scheduled_backup(
        user="root",
        backup_dir=tmp_path,
        database=["app", "analytics"],
        temp_dir=tmp_path / "schedule-temp",
        interval_seconds=60,
        compress=True,
        include_database_statements=True,
        hex_blob=True,
        retention_enabled=False,
        keep_last=None,
        keep_days=None,
        match_pattern="*.sql.gz",
        stop_on_failure=False,
    )

    manager = FakeManager.instances[-1]
    scheduler = FakeScheduler.instances[-1]
    assert manager.dump.databases == ["app", "analytics"]
    assert manager.dump.temp_dir == tmp_path / "schedule-temp"
    assert manager.dump.compress is True
    assert manager.dump.extra_options == ["--databases", "--quick", "--hex-blob"]
    assert manager.retention.enabled is False
    assert manager.retention.keep_last is None
    assert manager.retention.keep_days is None
    assert manager.retention.match_pattern == "*.sql.gz"
    assert scheduler.config.interval_seconds == 60
    assert scheduler.stop_on_failure is False


def test_scheduled_backup_helper_omitted_keep_last_disables_keep_last(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class FakeManager:
        instances = []

        def __init__(self, connection, dump, retention=None, logger=None) -> None:
            self.retention = retention
            self.__class__.instances.append(self)

    class FakeScheduler:
        def __init__(self, manager, config, logger=None) -> None:
            self.manager = manager

        async def run_forever(self, *, stop_on_failure: bool = False) -> None:
            return None

    monkeypatch.setattr(helper, "MySQLBackupManager", FakeManager)
    monkeypatch.setattr(helper, "SchedulerService", FakeScheduler)

    helper.scheduled_backup(
        user="root",
        backup_dir=tmp_path,
        database="app",
        interval_seconds=60,
        keep_days=7,
    )

    retention = FakeManager.instances[-1].retention
    assert retention.keep_last is None
    assert retention.keep_days == 7


def test_verify_checksum_supports_md5(tmp_path: Path) -> None:
    backup = tmp_path / "backup.sql"
    backup.write_text("SELECT 1;", encoding="utf-8")
    digest = helper.compute_checksum(backup, "md5")
    backup.with_name(f"{backup.name}.md5").write_text(
        f"{digest}  {backup.name}\n",
        encoding="utf-8",
    )

    helper.verify_checksum(backup, "md5")
