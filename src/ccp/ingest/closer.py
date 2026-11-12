"""Close sessions that have gone quiet.

A session is *open* while chunks keep arriving. If nothing arrives for
``CCP_SESSION_IDLE_MINUTES`` (a developer closed the laptop, the SessionEnd hook never ran, the
network dropped for good), it is marked *closed* so the data shows a finished session, and a
final index job is queued. A late chunk reopens the session (see ``repository.py``).

This module runs the closing on a timer inside the API process. The database work itself is
:meth:`ccp.ingest.repository.PostgresRepository.close_idle_sessions`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import Protocol

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("ccp.ingest.closer")

# Check at least this often (seconds), but not more often than every few seconds.
MAX_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 5


class IdleSessionCloser(Protocol):
    """The part of the repository the timer needs."""

    def close_idle_sessions(self, idle_minutes: int) -> list[str]:
        """Close sessions idle for ``idle_minutes`` and return their ids."""


def check_interval_seconds(idle_minutes: int) -> int:
    """How often to look for idle sessions: every minute, or sooner for very short timeouts.

    The check must run well within the idle period, so a 1-minute timeout is checked every
    30 seconds, a 30-minute timeout every 60 seconds.
    """
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, idle_minutes * 30))


async def run_periodically(
    closer: IdleSessionCloser, idle_minutes: int, interval_seconds: float
) -> None:
    """Close idle sessions again and again, forever (until the task is cancelled).

    A failure (database down, an unexpected error) is logged and the loop carries on, so one bad
    moment does not stop sessions from ever being closed.
    """
    while True:
        try:
            closed = await run_in_threadpool(closer.close_idle_sessions, idle_minutes)
            if closed:
                logger.info("closed %d idle session(s): %s", len(closed), ", ".join(closed))
        except Exception:  # noqa: BLE001  (deliberate: the loop must survive any failure)
            logger.exception("closing idle sessions failed; will try again")
        await asyncio.sleep(interval_seconds)


def closer_lifespan(
    closer: IdleSessionCloser, idle_minutes: int, interval_seconds: float
) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    """Return a Starlette ``lifespan`` that runs the closer while the server is up.

    The background task starts when the server starts and is cancelled when it stops.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        task = asyncio.create_task(run_periodically(closer, idle_minutes, interval_seconds))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return lifespan
