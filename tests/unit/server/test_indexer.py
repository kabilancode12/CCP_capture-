"""Tests for the indexer thread and the after-index callbacks."""

import threading
import time
from collections.abc import Callable
from typing import cast

from ccp.index.worker import ChunkStore, JobRepository
from ccp.server.indexer import CallbackRunner, IndexerThread

from .conftest import FakeJobs, FakeStore


def wait_for(condition: object, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(condition) and condition():
            return True
        time.sleep(0.01)
    return False


def make(
    jobs: FakeJobs, callbacks: list[Callable[[str], None]] | None = None, poll: float = 0.01
) -> IndexerThread:
    return IndexerThread(
        cast(ChunkStore, FakeStore()),
        cast(JobRepository, jobs),
        poll_seconds=poll,
        callbacks=callbacks or [],
    )


def test_queued_jobs_are_indexed_and_callbacks_get_the_session_id() -> None:
    jobs = FakeJobs()
    called: list[str] = []
    indexer = make(jobs, [called.append])
    indexer.start()
    try:
        jobs.queue("s1")
        jobs.queue("s2")
        assert wait_for(lambda: sorted(called) == ["s1", "s2"])
        assert sorted(jobs.done) == ["s1", "s2"]
        assert sorted(jobs.refreshed) == ["s1", "s2"]
        assert sorted(jobs.test_outputs) == ["s1", "s2"]  # calls pass through the wrapper
    finally:
        indexer.stop()
    assert not indexer.running


def test_without_callbacks_indexing_still_works() -> None:
    jobs = FakeJobs()
    indexer = make(jobs)
    indexer.start()
    try:
        jobs.queue("s1")
        assert wait_for(lambda: jobs.done == ["s1"])
    finally:
        indexer.stop()


def test_a_failing_callback_does_not_stop_the_others_or_the_indexer() -> None:
    jobs = FakeJobs()
    called: list[str] = []

    def bad(session_id: str) -> None:
        raise RuntimeError("boom")

    indexer = make(jobs, [bad, called.append])
    indexer.start()
    try:
        jobs.queue("s1")
        assert wait_for(lambda: called == ["s1"])
        jobs.queue("s2")
        assert wait_for(lambda: called == ["s1", "s2"])
    finally:
        indexer.stop()


def test_a_slow_callback_does_not_hold_up_indexing() -> None:
    jobs = FakeJobs()
    release = threading.Event()
    started = threading.Event()

    def slow(session_id: str) -> None:
        started.set()
        release.wait(5)

    indexer = make(jobs, [slow])
    indexer.start()
    try:
        jobs.queue("s1")
        assert started.wait(5)
        jobs.queue("s2")
        assert wait_for(lambda: jobs.done == ["s1", "s2"])  # indexed while s1's callback waits
    finally:
        release.set()
        indexer.stop()


def test_the_same_waiting_session_is_called_once() -> None:
    called: list[str] = []
    gate = threading.Event()

    def callback(session_id: str) -> None:
        gate.wait(5)
        called.append(session_id)

    runner = CallbackRunner([callback])
    runner.start()
    runner.submit("a")  # taken at once and blocks on the gate
    assert wait_for(lambda: runner._queue.empty())
    runner.submit("b")
    runner.submit("b")  # already waiting
    gate.set()
    assert wait_for(lambda: called == ["a", "b"])
    runner.stop()
    assert called == ["a", "b"]


def test_a_database_outage_does_not_stop_the_worker() -> None:
    jobs = FakeJobs()
    jobs.fail_claims = 2
    indexer = make(jobs)
    indexer.start()
    try:
        jobs.queue("s1")
        assert wait_for(lambda: jobs.done == ["s1"])
        assert indexer.running
    finally:
        indexer.stop()


def test_an_unexpected_failure_restarts_the_worker() -> None:
    jobs = FakeJobs()
    jobs.crash_claims = 1
    indexer = make(jobs)
    indexer.start()
    try:
        jobs.queue("s1")
        assert wait_for(lambda: jobs.done == ["s1"])
    finally:
        indexer.stop()


def test_stop_returns_quickly_even_when_idle_with_a_long_poll() -> None:
    jobs = FakeJobs()
    indexer = make(jobs, [lambda s: None], poll=60.0)
    indexer.start()
    time.sleep(0.1)
    started = time.monotonic()
    indexer.stop()
    assert time.monotonic() - started < 3
    assert not indexer.running


def test_a_job_that_is_running_when_stopping_is_finished_first() -> None:
    jobs = FakeJobs()
    inside = threading.Event()
    release = threading.Event()
    original = jobs.refresh_session

    def slow_refresh(session_id: str) -> None:
        inside.set()
        release.wait(5)
        original(session_id)

    jobs.refresh_session = slow_refresh  # type: ignore[method-assign]
    indexer = make(jobs)
    indexer.start()
    jobs.queue("s1")
    jobs.queue("s2")
    assert inside.wait(5)
    stopper = threading.Thread(target=indexer.stop)
    stopper.start()
    time.sleep(0.1)
    release.set()
    stopper.join(5)
    assert jobs.done == ["s1"]  # the running job completed; the next one was not started
    assert jobs.queued == ["s2"]
