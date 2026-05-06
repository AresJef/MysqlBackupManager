"""Checksum helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal


ChecksumName = Literal["sha256", "md5"]


def compute_checksum(path: Path, algorithm: ChecksumName = "sha256") -> str:
    """Compute a checksum for a file."""

    digest = hashlib.new(algorithm)
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum_file(path: Path, algorithm: ChecksumName = "sha256") -> tuple[Path, str]:
    """Write a checksum sidecar file and return its path and digest."""

    checksum = compute_checksum(path, algorithm)
    checksum_file = path.with_name(f"{path.name}.{algorithm}")
    checksum_file.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    return checksum_file, checksum

