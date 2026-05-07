"""Checksum helpers for backup artifacts.

Backups can optionally produce a sidecar checksum file next to the SQL or gzip
artifact. These helpers are intentionally small and standalone so applications
can also verify checksums before restore without constructing a manager.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal


ChecksumName = Literal["sha256", "md5"]


def compute_checksum(path: Path, algorithm: ChecksumName = "sha256") -> str:
    """Return the hex digest for ``path`` using ``algorithm``.

    :param path: File to read in binary mode.
    :param algorithm: Hash algorithm name. Supported values are ``"sha256"`` and ``"md5"``.
    :return: Lowercase hexadecimal checksum digest.
    :raises FileNotFoundError: If ``path`` does not exist.
    :raises ValueError: If ``algorithm`` is not supported by ``hashlib``.

    ## Example:
    ```python
    from pathlib import Path
    path = Path("checksum-example.txt")
    path.write_text("hello", encoding="utf-8")
    # 5
    len(compute_checksum(path, "sha256"))
    # 64
    path.unlink()
    ```
    """

    digest = hashlib.new(algorithm)
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum_file(path: Path, algorithm: ChecksumName = "sha256") -> tuple[Path, str]:
    """Write and return a checksum sidecar for ``path``.

    :param path: Backup artifact to checksum.
    :param algorithm: Hash algorithm name. Supported values are ``"sha256"`` and ``"md5"``.
    :return: Tuple of ``(checksum_file, checksum)`` where ``checksum_file`` is the sidecar path and ``checksum`` is the hex digest.
    :raises FileNotFoundError: If ``path`` does not exist.
    :raises OSError: If the sidecar cannot be written.

    ## Example:
    ```python
    from pathlib import Path
    path = Path("backup.sql")
    path.write_text("SELECT 1;", encoding="utf-8")
    # 9
    checksum_file, checksum = write_checksum_file(path)
    checksum_file.name
    # 'backup.sql.sha256'
    checksum_file.unlink(); path.unlink()
    ```
    """

    checksum = compute_checksum(path, algorithm)
    checksum_file = path.with_name(f"{path.name}.{algorithm}")
    checksum_file.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    return checksum_file, checksum

