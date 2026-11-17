"""The job queue under failure and concurrency (real PostgreSQL, see conftest.py).

A worker that died leaves its job 'running'; the next claim puts it back. Two workers on the same
session take turns (an advisory lock per session). A job put back in the queue at the moment the
API queues a new one is closed instead of failing.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from tests.integration.story import SESSION, index_story

from ccp.db.connection import connect
from ccp.index.parser import parse_line
from ccp.index.rebuild import RebuildRepository
from ccp.index.repository import (
    LOCK_SESSION,
    MAX_ATTEMPTS,
    STALE_JOB_MINUTES,
    IndexRepository,
)
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore

pytestmark = pytest.mark.integration

OTHER = "sess-other"


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _execute(database: DatabaseSettings, sql: str, parameters: tuple[Any, ...] = ()) -> None:
    connection = connect(database)
    try:
        connection.cursor().execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


def _job(database: DatabaseSettings, session: str, status: str, attempts: int, age: int) -> int:
    """Add an index job; ``age`` is how many minutes ago it was created and (if running) taken."""
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO job (job_type, session_id, status, attempts, created_at, started_at) "
            "VALUES ('index_session', %s, %s, %s, now() - %s * interval '1 minute', "
            "        CASE WHEN %s = 'running' THEN now() - %s * interval '1 minute' END) "
            "RETURNING job_id",
            (session, status, attempts, age, status, age),
        )
        job_id = int(cursor.fetchone()[0])
        connection.commit()
        return job_id
    finally:
        connection.close()


@pytest.fixture
def sessions(database: DatabaseSettings) -> DatabaseSettings:
    _execute(database, "INSERT INTO session (session_id) VALUES (%s), (%s)", (SESSION, OTHER))
    return database


# ------------------------------------------------------------------------ jobs of dead workers


def test_a_claimed_job_records_when_it_was_taken(sessions: DatabaseSettings) -> None:
    _job(sessions, SESSION, "queued", 0, 0)

    assert IndexRepository(sessions).claim_job() is not None

    rows = _rows(sessions, "SELECT status, started_at > now() - interval '1 minute' FROM job")
    assert rows == [("running", True)]


def test_a_job_whose_worker_died_is_queued_again_and_taken(sessions: DatabaseSettings) -> None:
    stale = _job(sessions, SESSION, "running", 1, STALE_JOB_MINUTES + 5)

    job = IndexRepository(sessions).claim_job()

    assert job is not None
    assert (job.job_id, job.session_id, job.attempts) == (stale, SESSION, 2)
    rows = _rows(sessions, "SELECT status, started_at > now() - interval '1 minute' FROM job")
    assert rows == [("running", True)]


def test_a_job_that_is_still_within_the_timeout_is_left_alone(sessions: DatabaseSettings) -> None:
    _job(sessions, SESSION, "running", 1, STALE_JOB_MINUTES - 5)

    assert IndexRepository(sessions).claim_job() is None
    assert _rows(sessions, "SELECT status, attempts FROM job") == [("running", 1)]


def test_a_dead_job_with_no_attempts_left_is_failed(sessions: DatabaseSettings) -> None:
    _job(sessions, SESSION, "running", MAX_ATTEMPTS, STALE_JOB_MINUTES + 5)

    assert IndexRepository(sessions).claim_job() is None
    rows = _rows(sessions, "SELECT status, finished_at IS NOT NULL FROM job")
    assert rows == [("failed", True)]


def test_a_dead_job_is_closed_when_its_session_already_has_a_queued_job(
    sessions: DatabaseSettings,
) -> None:
    stale = _job(sessions, SESSION, "running", 1, STALE_JOB_MINUTES + 5)
    queued = _job(sessions, SESSION, "queued", 0, 1)

    job = IndexRepository(sessions).claim_job()

    assert job is not None and job.job_id == queued
    rows = _rows(sessions, "SELECT job_id, status FROM job ORDER BY job_id")
    assert rows == [(stale, "done"), (queued, "running")]


def test_two_dead_jobs_of_one_session_give_one_queued_job(sessions: DatabaseSettings) -> None:
    first = _job(sessions, SESSION, "running", 1, STALE_JOB_MINUTES + 9)
    second = _job(sessions, SESSION, "running", 1, STALE_JOB_MINUTES + 5)

    job = IndexRepository(sessions).claim_job()

    assert job is not None and job.job_id == first
    rows = _rows(sessions, "SELECT job_id, status FROM job ORDER BY job_id")
    assert rows == [(first, "running"), (second, "done")]


def test_a_job_taken_before_migration_014_counts_from_its_creation(
    sessions: DatabaseSettings,
) -> None:
    stale = _job(sessions, SESSION, "running", 1, STALE_JOB_MINUTES + 5)
    _execute(sessions, "UPDATE job SET started_at = NULL")

    job = IndexRepository(sessions).claim_job()

    assert job is not None and job.job_id == stale


def test_dead_jobs_of_other_kinds_are_not_touched(sessions: DatabaseSettings) -> None:
    _execute(
        sessions,
        "INSERT INTO job (job_type, session_id, status, attempts, started_at) "
        "VALUES ('distil_session', %s, 'running', 1, now() - interval '1 day')",
        (SESSION,),
    )

    assert IndexRepository(sessions).claim_job() is None
    assert _rows(sessions, "SELECT status FROM job") == [("running",)]


# ------------------------------------------------------------- a job queued again at a bad time


def _in_thread(work: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    """Start ``work`` in a thread; the list receives its result (or the exception it raised)."""
    outcome: list[Any] = []

    def target() -> None:
        try:
            outcome.append(work())
        except Exception as error:  # noqa: BLE001  (handed to the test)
            outcome.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


def _wait_until_blocked(database: DatabaseSettings, thread: threading.Thread) -> None:
    """Wait until another connection is waiting for a lock (the work in ``thread`` blocked)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        waiting = _rows(
            database,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'",
        )
        if waiting[0][0] > 0:
            return
        assert thread.is_alive(), "the work finished without waiting"
        time.sleep(0.05)
    raise AssertionError("the work never waited for the lock")


def test_a_failed_job_is_closed_when_the_api_queues_a_new_one_at_the_same_moment(
    sessions: DatabaseSettings,
) -> None:
    """The retry's check sees no queued job, but the API's new job wins the unique index."""
    failed = _job(sessions, SESSION, "running", 1, 0)
    api = connect(sessions)  # the API, in the middle of queueing a job for the same session
    api.cursor().execute(
        "INSERT INTO job (job_type, session_id) VALUES ('index_session', %s)", (SESSION,)
    )

    thread, outcome = _in_thread(lambda: IndexRepository(sessions).fail_job(failed))
    _wait_until_blocked(sessions, thread)
    api.commit()
    api.close()
    thread.join(timeout=10)

    assert outcome == [False]
    rows = _rows(sessions, "SELECT status FROM job ORDER BY job_id")
    assert rows == [("done",), ("queued",)]


def test_a_failed_job_with_a_queued_job_already_there_is_failed_as_before(
    sessions: DatabaseSettings,
) -> None:
    failed = _job(sessions, SESSION, "running", 1, 0)
    _job(sessions, SESSION, "queued", 0, 0)

    assert IndexRepository(sessions).fail_job(failed) is False

    assert _rows(sessions, "SELECT status FROM job ORDER BY job_id") == [("failed",), ("queued",)]


# ------------------------------------------------------------------- one session at a time


@pytest.fixture
def session_locked(sessions: DatabaseSettings) -> Iterator[Any]:
    """Another worker holds the lock of SESSION until the test commits the connection."""
    holder = connect(sessions)
    holder.cursor().execute(LOCK_SESSION, (SESSION,))
    try:
        yield holder
    finally:
        holder.close()


def _save_one_event(database: DatabaseSettings, session: str) -> None:
    entry = parse_line('{"type": "user", "uuid": "u1", "message": {"content": "hi"}}')
    key = f"sessions/{session}/main/000000000000.jsonl"
    IndexRepository(database).save_chunk(session, key, [(1, entry)])


@pytest.mark.parametrize(
    "work",
    [
        lambda database: _save_one_event(database, SESSION),
        lambda database: IndexRepository(database).refresh_session(SESSION),
        lambda database: RebuildRepository(database).reset(SESSION),
    ],
    ids=["save_chunk", "refresh_session", "rebuild reset"],
)
def test_work_on_a_session_waits_for_the_other_worker_on_it(
    sessions: DatabaseSettings,
    session_locked: Any,
    work: Callable[[DatabaseSettings], None],
) -> None:
    thread, outcome = _in_thread(lambda: work(sessions))

    _wait_until_blocked(sessions, thread)
    assert thread.is_alive()
    session_locked.commit()  # the other worker finishes
    thread.join(timeout=10)

    assert outcome == [None]


def test_work_on_another_session_does_not_wait(
    sessions: DatabaseSettings, session_locked: Any
) -> None:
    _save_one_event(sessions, OTHER)  # returns at once although SESSION is locked

    assert _rows(sessions, "SELECT session_id FROM event") == [(OTHER,)]


def test_a_refresh_during_another_workers_refresh_waits_and_then_succeeds(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    """The collision seen without the lock: both workers rewrite session_model_cost.

    The other worker is half way through its refresh: it has replaced the session's cost rows but
    not yet committed. Without the lock, this refresh would wait for those rows and then fail on
    the primary key; with it, it waits for the whole refresh and then simply does it again.
    """
    index_story(database, object_store)
    rebuild = RebuildRepository(database)
    expected = rebuild.fingerprints(SESSION)
    other = connect(database)
    cursor = other.cursor()
    cursor.execute(LOCK_SESSION, (SESSION,))
    cursor.execute("DELETE FROM session_model_cost WHERE session_id = %s", (SESSION,))
    cursor.execute(
        "INSERT INTO session_model_cost (session_id, model) VALUES (%s, 'model-a'), "
        "(%s, 'helper-model')",
        (SESSION, SESSION),
    )

    thread, outcome = _in_thread(lambda: IndexRepository(database).refresh_session(SESSION))
    _wait_until_blocked(database, thread)
    other.commit()
    other.close()
    thread.join(timeout=10)

    assert outcome == [None]
    assert rebuild.fingerprints(SESSION) == expected
