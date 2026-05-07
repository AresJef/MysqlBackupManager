"""Compression helpers for SQL dump files.

The library currently supports gzip because it is universally available and
streams well for large database dumps. Helpers use temporary files where needed
so interrupted compression does not leave a half-written final artifact.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


def gzip_file(path: Path, *, remove_original: bool = True) -> Path:
    """Compress ``path`` to ``path`` plus ``.gz`` and return the gzip path.

    :param path: Existing file to compress.
    :param remove_original: Delete ``path`` after the gzip file has been successfully written and atomically moved into place.
    :return: Path to the final gzip file.
    :raises FileNotFoundError: If ``path`` does not exist.
    :raises OSError: If compression, replacement, or deletion fails.

    ## Example:
    ```python
    from pathlib import Path
    path = Path("example.sql")
    path.write_text("SELECT 1;", encoding="utf-8")
    # 9
    gz_path = gzip_file(path)
    gz_path.name
    # 'example.sql.gz'
    gz_path.unlink()
    ```
    """

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


def open_sql_input(path: Path, *, decompress: bool = True) -> BinaryIO:
    """Open ``path`` as a binary SQL input stream for restore.

    :param path: Existing ``.sql`` or ``.sql.gz`` file.
    :param decompress: When true and ``path`` ends with ``.sql.gz``, return a gzip decompression stream. Otherwise return a normal binary file stream.
    :return: A binary file-like object. Use it as a context manager.
    :raises FileNotFoundError: If ``path`` does not exist.
    :raises OSError: If the file cannot be opened.

    ## Example:
    ```python
    from pathlib import Path
    path = Path("plain.sql")
    path.write_text("SELECT 1;", encoding="utf-8")
    # 9
    with open_sql_input(path) as stream:
        stream.read(6)
    # b'SELECT'
    path.unlink()
    ```
    """

    if decompress and path.suffixes[-2:] == [".sql", ".gz"]:
        return gzip.open(path, "rb")
    return path.open("rb")

