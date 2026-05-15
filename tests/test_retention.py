from __future__ import annotations

import os
import time
from pathlib import Path

from mysql_backup_manager.config import RetentionConfig
from mysql_backup_manager.retention import RetentionService


def _touch(path: Path, age_days: int) -> None:
    path.write_text("backup", encoding="utf-8")
    timestamp = time.time() - age_days * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp))


def test_retention_keeps_last_and_deletes_older_files(tmp_path: Path) -> None:
    newest = tmp_path / "newest.sql"
    second = tmp_path / "second.sql"
    old = tmp_path / "old.sql"
    _touch(newest, 0)
    _touch(second, 1)
    _touch(old, 60)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=2, keep_days=None),
    ).cleanup()

    assert result.success
    assert old in result.deleted_files
    assert not old.exists()
    assert newest.exists()
    assert second.exists()


def test_retention_keep_days_without_keep_last_deletes_only_by_age(tmp_path: Path) -> None:
    newest = tmp_path / "newest.sql"
    second = tmp_path / "second.sql"
    old = tmp_path / "old.sql"
    _touch(newest, 0)
    _touch(second, 1)
    _touch(old, 8)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=None, keep_days=7),
    ).cleanup()

    assert result.success
    assert newest.exists()
    assert second.exists()
    assert not old.exists()
    assert old in result.deleted_files


def test_retention_deletes_files_that_exceed_either_policy(tmp_path: Path) -> None:
    recent = tmp_path / "recent.sql"
    old_but_last = tmp_path / "old_but_last.sql"
    delete_me = tmp_path / "delete_me.sql"
    _touch(recent, 1)
    _touch(old_but_last, 40)
    _touch(delete_me, 50)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=2, keep_days=30),
    ).cleanup()

    assert result.success
    assert recent.exists()
    assert not old_but_last.exists()
    assert not delete_me.exists()
    assert old_but_last in result.deleted_files
    assert delete_me in result.deleted_files


def test_retention_does_not_follow_files_outside_output_dir(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.sql"
    outside.write_text("outside", encoding="utf-8")
    try:
        link = tmp_path / "link.sql"
        link.symlink_to(outside)

        result = RetentionService(tmp_path, RetentionConfig(keep_last=0, keep_days=None)).cleanup()

        assert result.success
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_retention_keep_last_counts_backup_artifacts_not_checksum_sidecars(tmp_path: Path) -> None:
    newest = tmp_path / "newest.sql.gz"
    second = tmp_path / "second.sql.gz"
    old = tmp_path / "old.sql.gz"
    _touch(newest, 0)
    _touch(second, 1)
    _touch(old, 2)
    _touch(newest.with_name(f"{newest.name}.sha256"), 0)
    _touch(second.with_name(f"{second.name}.sha256"), 1)
    _touch(old.with_name(f"{old.name}.sha256"), 2)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=2, keep_days=None, match_pattern="*.sql.gz"),
    ).cleanup()

    assert result.success
    assert newest.exists()
    assert newest.with_name(f"{newest.name}.sha256").exists()
    assert second.exists()
    assert second.with_name(f"{second.name}.sha256").exists()
    assert not old.exists()
    assert not old.with_name(f"{old.name}.sha256").exists()
    assert old in result.deleted_files
    assert old.with_name(f"{old.name}.sha256") in result.deleted_files


def test_retention_keeps_all_when_keep_last_and_keep_days_are_disabled(tmp_path: Path) -> None:
    newest = tmp_path / "newest.sql"
    old = tmp_path / "old.sql"
    _touch(newest, 0)
    _touch(old, 90)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=None, keep_days=None),
    ).cleanup()

    assert result.success
    assert result.deleted_files == []
    assert newest.exists()
    assert old.exists()
    assert set(result.kept_files) == {newest, old}


def test_retention_deletes_orphan_checksum_sidecars_when_limits_enabled(tmp_path: Path) -> None:
    orphan = tmp_path / "missing.sql.gz.sha256"
    _touch(orphan, 10)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=5, keep_days=None, match_pattern="*.sql*"),
    ).cleanup()

    assert result.success
    assert not orphan.exists()
    assert orphan in result.deleted_files


def test_retention_keeps_orphan_checksum_sidecars_when_limits_disabled(tmp_path: Path) -> None:
    orphan = tmp_path / "missing.sql.gz.sha256"
    _touch(orphan, 10)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=None, keep_days=None, match_pattern="*.sql*"),
    ).cleanup()

    assert result.success
    assert orphan.exists()
    assert orphan in result.kept_files


def test_retention_service_accepts_string_output_dir(tmp_path: Path) -> None:
    backup = tmp_path / "backup.sql"
    _touch(backup, 0)

    result = RetentionService(
        str(tmp_path),
        RetentionConfig(keep_last=None, keep_days=None),
    ).cleanup()

    assert result.success
    assert backup in result.kept_files


def test_retention_deletes_orphan_sidecar_when_primary_pattern_excludes_sidecars(tmp_path: Path) -> None:
    orphan = tmp_path / "missing.sql.gz.sha256"
    _touch(orphan, 10)

    result = RetentionService(
        tmp_path,
        RetentionConfig(keep_last=5, keep_days=None, match_pattern="*.sql.gz"),
    ).cleanup()

    assert result.success
    assert not orphan.exists()
    assert orphan in result.deleted_files
