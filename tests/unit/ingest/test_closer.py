import asyncio
import time
from contextlib import suppress

import pytest
from starlette.testclient import TestClient

from ccp.ingest.app import IngestService
from ccp.ingest.closer import check_interval_seconds, closer_lifespan, run_periodically
from ccp.ingest.repository import RepositoryError
from ccp.settings import Secret

from .conftest import FakeRepository, FakeStore


class FakeCloser:
    """Counts how often it is asked to close sessions; the first calls can be made to fail."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[int] = []
        self._failures = failures

    def close_idle_sessions(self, idle_minutes: int) -> list[str]:
        self.calls.append(idle_minutes)
        if len(self.calls) <= self._failures:
            raise RepositoryError("database down")
        return ["s1"] if len(self.calls) == self._failures + 1 else []


async def _run_for(closer: FakeCloser, seconds: float, interval: float = 0.01) -> None:
    """Run the periodic loop for a short time, then cancel it (as the server does at shutdown)."""
    task = asyncio.create_task(run_periodically(closer, 30, interval))
    await asyncio.sleep(seconds)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("idle_minutes", "expected"),
    [(1, 30), (2, 60), (30, 60), (1440, 60)],  # every minute at most; sooner for short timeouts
)
def test_check_interval_stays_well_inside_the_idle_period(idle_minutes: int, expected: int) -> None:
    assert check_interval_seconds(idle_minutes) == expected


def test_the_loop_closes_sessions_again_and_again_with_the_configured_timeout() -> None:
    closer = FakeCloser()
    asyncio.run(_run_for(closer, 0.15))
    assert len(closer.calls) >= 3
    assert set(closer.calls) == {30}  # the idle minutes are passed through


def test_a_failure_does_not_stop_the_loop() -> None:
    closer = FakeCloser(failures=2)  # the first two calls raise
    asyncio.run(_run_for(closer, 0.15))
    assert len(closer.calls) >= 4  # it kept going after the errors


def test_the_closer_runs_while_the_server_is_up_and_stops_with_it(
    store: FakeStore, repository: FakeRepository
) -> None:
    closer = FakeCloser()
    service = IngestService(
        token=Secret("t" * 24), store=store, repository=repository, max_chunk_bytes=1024
    )
    app = service.build_app(lifespan=closer_lifespan(closer, 30, 0.01))

    with TestClient(app) as client:
        time.sleep(0.2)
        assert client.get("/health").status_code == 200  # the API works alongside the closer
        assert len(closer.calls) >= 2

    calls_at_shutdown = len(closer.calls)
    time.sleep(0.1)
    assert len(closer.calls) == calls_at_shutdown  # nothing runs after shutdown
