"""Enumerations used by mysql-backup-manager."""

from __future__ import annotations

from enum import StrEnum


class CompressionFormat(StrEnum):
    """Supported compression formats."""

    GZIP = "gzip"


class ChecksumAlgorithm(StrEnum):
    """Supported checksum algorithms."""

    SHA256 = "sha256"
    MD5 = "md5"

