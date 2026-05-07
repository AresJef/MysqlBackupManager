"""Enumerations used by mysql-backup-manager."""

from __future__ import annotations

from enum import StrEnum


class CompressionFormat(StrEnum):
    """Supported backup compression formats.

    :param value: Enum value to construct, currently only ``"gzip"``.
    :return: A ``CompressionFormat`` enum member.

    ## Example:
    ```python
    CompressionFormat("gzip") is CompressionFormat.GZIP
    # True
    ```
    """

    GZIP = "gzip"


class ChecksumAlgorithm(StrEnum):
    """Supported checksum algorithms for backup sidecar files.

    :param value: Enum value to construct, either ``"sha256"`` or ``"md5"``.
    :return: A ``ChecksumAlgorithm`` enum member.

    ## Example:
    ```python
    ChecksumAlgorithm("sha256") is ChecksumAlgorithm.SHA256
    # True
    ```
    """

    SHA256 = "sha256"
    MD5 = "md5"

