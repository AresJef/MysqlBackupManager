from __future__ import annotations

import asyncio
import gzip
import importlib
import os
import time
from pathlib import Path

import pytest

from mysql_backup_manager.backup import BackupService, cleanup_stale_backup_temp_files, is_backup_temp_file
from mysql_backup_manager.config import DumpConfig, MySQLConnectionConfig
from mysql_backup_manager.exceptions import MySQLCommandError


backup_module = importlib.import_module("mysql_backup_manager.backup")


@pytest.fixture(autouse=True)
def isolate_default_backup_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Keep default backup temp files out of the real home directory during tests."""

    temp_dir = tmp_path_factory.mktemp("mysql-backup-manager-temp")
    monkeypatch.setenv("MYSQL_BACKUP_MANAGER_TEMP_DIR", str(temp_dir))


async def test_backup_uses_temp_file_and_cleans_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        output_file.write_text("partial", encoding="utf-8")
        raise MySQLCommandError("boom", stderr="failed")

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            command_timeout=12,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is False
    assert result.stderr == "failed"
    assert not list(tmp_path.glob("*.part"))
    assert not result.output_file.exists()


async def test_backup_passes_command_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen_timeout = None

    async def fake_run(command, output_file, **kwargs):
        nonlocal seen_timeout
        seen_timeout = kwargs["timeout"]
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            command_timeout=12,
            generate_checksum=False,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is True
    assert seen_timeout == 12
    assert "CREATE TABLE" in result.output_file.read_text(encoding="utf-8")


async def test_backup_stages_dump_in_configured_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output_dir = tmp_path / "backups"
    temp_dir = tmp_path / "manager-temp"
    seen_output_file: Path | None = None

    async def fake_run(command, output_file, **kwargs):
        nonlocal seen_output_file
        seen_output_file = output_file
        assert output_file.parent == temp_dir
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=output_dir,
            temp_dir=temp_dir,
            generate_checksum=False,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is True
    assert seen_output_file is not None
    assert result.output_file.parent == output_dir
    assert result.output_file.exists()
    assert not list(output_dir.glob("*.part"))
    assert not list(temp_dir.iterdir())


async def test_compressed_backup_stages_dump_and_gzip_in_configured_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output_dir = tmp_path / "backups"
    temp_dir = tmp_path / "manager-temp"

    async def fake_run(command, output_file, **kwargs):
        assert output_file.parent == temp_dir
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=output_dir,
            temp_dir=temp_dir,
            filename_template="{database}_{timestamp}.sql",
            timestamp_format="fixed",
            compress=True,
            generate_checksum=False,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is True
    assert result.output_file == output_dir / "app_fixed.sql"
    assert result.compressed_file == output_dir / "app_fixed.sql.gz"
    assert not result.output_file.exists()
    assert result.compressed_file.exists()
    with gzip.open(result.compressed_file, "rt", encoding="utf-8") as file:
        assert "CREATE TABLE" in file.read()
    assert not list(output_dir.glob("*.part"))
    assert not list(temp_dir.iterdir())


async def test_backup_publishes_checksum_sidecar_from_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output_dir = tmp_path / "backups"
    temp_dir = tmp_path / "manager-temp"

    async def fake_run(command, output_file, **kwargs):
        assert output_file.parent == temp_dir
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=output_dir,
            temp_dir=temp_dir,
            filename_template="{database}_{timestamp}.sql",
            timestamp_format="fixed",
            generate_checksum=True,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is True
    assert result.output_file == output_dir / "app_fixed.sql"
    assert result.checksum_file == output_dir / "app_fixed.sql.sha256"
    assert result.checksum_file.exists()
    assert result.checksum_file.read_text(encoding="utf-8").endswith("  app_fixed.sql\n")
    assert not list(output_dir.glob("*.part"))
    assert not list(temp_dir.iterdir())


async def test_compressed_backup_respects_overwrite_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        raise AssertionError("mysqldump should not run when target exists")

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    existing = tmp_path / "app_fixed.sql.gz"
    existing.write_text("existing", encoding="utf-8")
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            filename_template="{database}_{timestamp}.sql",
            timestamp_format="fixed",
            compress=True,
            overwrite=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is False
    assert result.compressed_file == existing
    assert "already exists" in (result.error or "")
    assert existing.read_text(encoding="utf-8") == "existing"


async def test_manager_sync_api_rejects_running_event_loop(tmp_path: Path) -> None:
    from mysql_backup_manager.backup import MySQLBackupManager

    manager = MySQLBackupManager(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    with pytest.raises(RuntimeError, match="Sync APIs cannot be called"):
        manager.backup_all_sync()


async def test_manager_backup_database_strips_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from mysql_backup_manager.backup import MySQLBackupManager

    async def fake_backup(database: str):
        assert database == "app"
        return object()

    manager = MySQLBackupManager(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=[" app "], output_dir=tmp_path),
    )
    monkeypatch.setattr(manager.backup_service, "backup_database", fake_backup)

    result = await manager.backup_database(" app ")

    assert result is not None


def test_public_time_helpers_are_no_longer_exported_from_backup_module() -> None:
    from mysql_backup_manager import utils

    assert backup_module.utc_now is utils.utc_now
    assert backup_module.elapsed_seconds is utils.elapsed_seconds


async def test_compressed_backup_respects_existing_uncompressed_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        raise AssertionError("mysqldump should not run when uncompressed staging target exists")

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    existing = tmp_path / "app_fixed.sql"
    existing.write_text("existing", encoding="utf-8")
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            filename_template="{database}_{timestamp}.sql",
            timestamp_format="fixed",
            compress=True,
            overwrite=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is False
    assert result.output_file == existing
    assert result.compressed_file is None
    assert "already exists" in (result.error or "")
    assert existing.read_text(encoding="utf-8") == "existing"


async def test_backup_fails_before_dump_when_database_is_not_visible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_database_exists(database: str) -> bool:
        assert database == "missing"
        return False

    async def fake_run(command, output_file, **kwargs):
        raise AssertionError("mysqldump should not run when preflight fails")

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=["missing"], output_dir=tmp_path),
    )
    monkeypatch.setattr(service, "database_exists", fake_database_exists)

    result = await service.backup_database("missing")

    assert result.success is False
    assert "does not exist or is not visible" in (result.error or "")
    assert not list(tmp_path.iterdir())


async def test_backup_uses_database_existence_preflight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen_database = None

    async def fake_database_exists(database: str) -> bool:
        nonlocal seen_database
        seen_database = database
        return True

    async def fake_visible_object_count(database: str) -> int:
        assert database == "app"
        return 1

    async def fake_run(command, output_file, **kwargs):
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=["app"], output_dir=tmp_path, generate_checksum=False),
    )
    monkeypatch.setattr(service, "database_exists", fake_database_exists)
    monkeypatch.setattr(service, "visible_object_count", fake_visible_object_count)

    result = await service.backup_database("app")

    assert result.success is True
    assert seen_database == "app"


async def test_backup_fails_before_dump_when_no_tables_or_views_are_visible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_database_exists(database: str) -> bool:
        assert database == "app"
        return True

    async def fake_visible_object_count(database: str) -> int:
        assert database == "app"
        return 0

    async def fake_run(command, output_file, **kwargs):
        raise AssertionError("mysqldump should not run when no objects are visible")

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="mysql_backup"),
        DumpConfig(databases=["app"], output_dir=tmp_path),
    )
    monkeypatch.setattr(service, "database_exists", fake_database_exists)
    monkeypatch.setattr(service, "visible_object_count", fake_visible_object_count)

    result = await service.backup_database("app")

    assert result.success is False
    assert "has no visible tables or views" in (result.error or "")
    assert "mysql_backup" in (result.error or "")
    assert not list(tmp_path.iterdir())


async def test_backup_fails_when_dump_contains_no_table_or_row_markers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_database_exists(database: str) -> bool:
        assert database == "app"
        return True

    async def fake_visible_object_count(database: str) -> int:
        assert database == "app"
        return 20

    async def fake_run(command, output_file, **kwargs):
        output_file.write_text("-- MySQL dump header only\n", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="mysql_backup"),
        DumpConfig(databases=["app"], output_dir=tmp_path),
    )
    monkeypatch.setattr(service, "database_exists", fake_database_exists)
    monkeypatch.setattr(service, "visible_object_count", fake_visible_object_count)

    result = await service.backup_database("app")

    assert result.success is False
    assert "produced no table definitions or row data" in (result.error or "")
    assert not list(tmp_path.iterdir())


async def test_backup_cancellation_removes_current_temp_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        output_file.write_text("partial", encoding="utf-8")
        raise asyncio.CancelledError

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.backup_database("app")

    assert not list(tmp_path.iterdir())


def test_cleanup_stale_backup_temp_files_deletes_only_old_hidden_part_files(tmp_path: Path) -> None:
    old_temp = tmp_path / ".app_20260101.sql.0123456789abcdef0123456789abcdef.part"
    young_temp = tmp_path / ".app_20260102.sql.fedcba9876543210fedcba9876543210.part"
    visible_part = tmp_path / "app_20260103.sql.part"
    unrelated = tmp_path / ".not-a-sql-temp.part"
    for path in (old_temp, young_temp, visible_part, unrelated):
        path.write_text("partial", encoding="utf-8")

    old_timestamp = time.time() - 120
    os.utime(old_temp, (old_timestamp, old_timestamp))

    assert is_backup_temp_file(old_temp) is True
    assert is_backup_temp_file(visible_part) is False

    deleted = cleanup_stale_backup_temp_files(tmp_path, older_than_seconds=60)

    assert deleted == [old_temp]
    assert not old_temp.exists()
    assert young_temp.exists()
    assert visible_part.exists()
    assert unrelated.exists()


async def test_backup_cleans_stale_temp_files_before_dump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stale_temp = tmp_path / ".app_20260101.sql.0123456789abcdef0123456789abcdef.part"
    stale_temp.write_text("partial", encoding="utf-8")
    old_timestamp = time.time() - 120
    os.utime(stale_temp, (old_timestamp, old_timestamp))

    async def fake_run(command, output_file, **kwargs):
        assert not stale_temp.exists()
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            generate_checksum=False,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
            stale_temp_file_age_seconds=60,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is True
    assert not stale_temp.exists()


async def test_backup_continues_when_stale_temp_cleanup_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        output_file.write_text("CREATE TABLE `app` (`id` int);", encoding="utf-8")
        return ""

    def fake_cleanup(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(backup_module, "run_command_to_file", fake_run)
    monkeypatch.setattr(backup_module, "cleanup_stale_backup_temp_files", fake_cleanup)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(
            databases=["app"],
            output_dir=tmp_path,
            generate_checksum=False,
            validate_database_exists=False,
            validate_database_has_objects=False,
            validate_dump_content=False,
        ),
    )

    result = await service.backup_database("app")

    assert result.success is True
