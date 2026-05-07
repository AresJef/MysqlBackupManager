from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from mysql_backup_manager.checksum import compute_checksum, write_checksum_file
from mysql_backup_manager.compression import gzip_file, open_sql_input


def test_checksum_generation(tmp_path: Path) -> None:
    path = tmp_path / "backup.sql"
    path.write_text("select 1;\n", encoding="utf-8")

    checksum_file, checksum = write_checksum_file(path, "sha256")

    assert checksum == compute_checksum(path, "sha256")
    assert checksum_file.name == "backup.sql.sha256"
    assert checksum_file.read_text(encoding="utf-8") == f"{checksum}  backup.sql\n"


def test_gzip_compression_and_decompression(tmp_path: Path) -> None:
    path = tmp_path / "backup.sql"
    path.write_text("select 1;\n", encoding="utf-8")

    compressed = gzip_file(path)

    assert compressed.name == "backup.sql.gz"
    assert compressed.exists()
    assert not path.exists()
    with gzip.open(compressed, "rt", encoding="utf-8") as file:
        assert file.read() == "select 1;\n"
    with open_sql_input(compressed) as file:
        assert file.read() == b"select 1;\n"


def test_helper_verify_checksum_rejects_empty_sidecar(tmp_path: Path) -> None:
    from mysql_backup_manager.helper import verify_checksum

    backup = tmp_path / "backup.sql.gz"
    backup.write_bytes(b"backup")
    backup.with_name(f"{backup.name}.sha256").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="empty or invalid"):
        verify_checksum(backup)

