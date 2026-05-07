"""Logging helpers."""

from __future__ import annotations

import logging


def get_logger(name: str = "mysql_backup_manager") -> logging.Logger:
    """Return a package logger without configuring global logging.

    :param name: Logger name to retrieve. Defaults to ``"mysql_backup_manager"``.
    :return: A standard-library ``logging.Logger`` instance.

    ## Example:
    ```python
    logger = get_logger()
    logger.name
    # 'mysql_backup_manager'
    ```
    """

    return logging.getLogger(name)

