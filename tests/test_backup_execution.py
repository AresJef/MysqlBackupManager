from __future__ import annotations

from pathlib import Path

import pytest

from mysql_backup_manager.backup import BackupService
from mysql_backup_manager.config import DumpConfig, MySQLConnectionConfig
from mysql_backup_manager.exceptions import MySQLCommandError


async def test_backup_uses_temp_file_and_cleans_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        output_file.write_text("partial", encoding="utf-8")
        raise MySQLCommandError("boom", stderr="failed")

    monkeypatch.setattr("mysql_backup_manager.backup.run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=["app"], output_dir=tmp_path, command_timeout=12),
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
        output_file.write_text("dump", encoding="utf-8")
        return ""

    monkeypatch.setattr("mysql_backup_manager.backup.run_command_to_file", fake_run)
    service = BackupService(
        MySQLConnectionConfig(user="root"),
        DumpConfig(databases=["app"], output_dir=tmp_path, command_timeout=12, generate_checksum=False),
    )

    result = await service.backup_database("app")

    assert result.success is True
    assert seen_timeout == 12
    assert result.output_file.read_text(encoding="utf-8") == "dump"



async def test_compressed_backup_respects_overwrite_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_run(command, output_file, **kwargs):
        raise AssertionError("mysqldump should not run when target exists")

    monkeypatch.setattr("mysql_backup_manager.backup.run_command_to_file", fake_run)
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
        DumpConfig(databases=["app"], output_dir=tmp_path),
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
