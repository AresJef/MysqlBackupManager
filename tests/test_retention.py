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


def test_retention_keeps_files_that_match_either_policy(tmp_path: Path) -> None:
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
    assert old_but_last.exists()
    assert not delete_me.exists()


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

