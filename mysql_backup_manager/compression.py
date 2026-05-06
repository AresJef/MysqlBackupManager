"""Compression helpers."""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path
from uuid import uuid4


def gzip_file(path: Path, *, remove_original: bool = True) -> Path:
    """Compress a file using gzip."""

    compressed_path = path.with_suffix(path.suffix + ".gz")
    temp_path = compressed_path.with_name(f".{compressed_path.name}.{uuid4().hex}.part")
    try:
        with path.open("rb") as source, gzip.open(temp_path, "wb") as target:
            shutil.copyfileobj(source, target)
        temp_path.replace(compressed_path)
    finally:
        temp_path.unlink(missing_ok=True)
    if remove_original:
        path.unlink()
    return compressed_path


def open_sql_input(path: Path, *, decompress: bool = True):
    """Open a SQL input file, optionally decompressing gzip files."""

    if decompress and path.suffixes[-2:] == [".sql", ".gz"]:
        return gzip.open(path, "rb")
    return path.open("rb")

