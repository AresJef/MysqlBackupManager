"""Internal utility helpers shared across services.

These functions keep timezone handling and sync/async guardrails consistent
without introducing a heavier abstraction layer. They are not required for most
application code, but remain importable for tests and advanced integrations.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    :return: A ``datetime`` whose ``tzinfo`` is ``datetime.timezone.utc``.
    Example:
    ```python
    utc_now().tzinfo is not None
    # True
    ```
    """

    return datetime.now(timezone.utc)


def elapsed_seconds(started_at: datetime, finished_at: datetime) -> float:
    """Return the duration in seconds between two datetimes.

    :param started_at: Start timestamp.
    :param finished_at: Finish timestamp.
    :return: Floating-point number of seconds between ``started_at`` and ``finished_at``.
    Example:
    ```python
    from datetime import datetime, timezone
    elapsed_seconds(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    # 2.0
    ```
    """

    return (finished_at - started_at).total_seconds()


def ensure_no_running_loop() -> None:
    """Raise if a synchronous convenience API is called inside an event loop.

    :return: None when no event loop is running in the current thread.
    :raises RuntimeError: If an asyncio event loop is already running.
    Example:
    ```python
    ensure_no_running_loop() is None
    # True
    ```
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Sync APIs cannot be called from a running event loop; use the async API instead")
