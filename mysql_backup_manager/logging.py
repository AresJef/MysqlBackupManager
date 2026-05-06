"""Logging helpers."""

from __future__ import annotations

import logging


def get_logger(name: str = "mysql_backup_manager") -> logging.Logger:
    """Return a package logger without configuring global logging."""

    return logging.getLogger(name)

