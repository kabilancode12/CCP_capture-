"""Closing idle sessions, against a real PostgreSQL (see conftest.py)."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ccp.db.connection import connect
from ccp.ingest.repository import PostgresRepository
from ccp.ingest.server import main
from ccp.settings import DatabaseSettings

pytestmark = pytest.mark.integration

REAL_MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
IDLE_MINUTES = 30


@pytest.fixture
def repository(database: DatabaseSettings) -> PostgresRepository:
    return PostgresRepository(database)


def _execute(database: DatabaseSettings, sql: str) -> None:
    connection = connect(database)
    try:
        connection.cursor().execute(sql)
        connection.commit()
    finally:
        connection.close()


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _add_session(
    database: DatabaseSettings, session_id: str, minutes_ago: int, **extra: str
) -> None:
    """Insert an open session whose last activity was ``minutes_ago`` minutes ago."""
    columns = "".join(f", {name}" for name in extra)
    values = "".join(f", '{value}'" for value in extra.values())
    _execute(
        database,
        f"INSERT INTO session (session_id, last_activity_at{columns}) "  # noqa: S608 (test data)
        f"VALUES ('{session_id}', now() - interval '{minutes_ago} minutes'{values})",
    )


def test_a_quiet_session_is_closed_and_a_final_index_job_is_queued(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "quiet", minutes_ago=120)

    assert repository.close_idle_sessions(IDLE_MINUTES) == ["quiet"]

    status, ended_at, last_activity, reason = _rows(
        database, "SELECT status, ended_at, last_activity_at, end_reason FROM session"
    )[0]
    assert status == "closed"
    assert ended_at == last_activity  # the session ended when it last had activity
    assert reason == "idle_timeout"
    assert _rows(database, "SELECT job_type, session_id, status FROM job") == [
        ("index_session", "quiet", "queued")
    ]


def test_a_recent_session_is_left_open(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "busy", minutes_ago=1)
    assert repository.close_idle_sessions(IDLE_MINUTES) == []
    assert _rows(database, "SELECT status FROM session") == [("open",)]
    assert _rows(database, "SELECT count(*) FROM job") == [(0,)]


def test_the_boundary_is_the_idle_period(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "just-inside", minutes_ago=IDLE_MINUTES - 1)
    _add_session(database, "just-outside", minutes_ago=IDLE_MINUTES + 1)
    assert repository.close_idle_sessions(IDLE_MINUTES) == ["just-outside"]


def test_already_closed_sessions_are_not_touched_or_requeued(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "done", minutes_ago=500, status="closed")
    assert repository.close_idle_sessions(IDLE_MINUTES) == []
    assert _rows(database, "SELECT count(*) FROM job") == [(0,)]


def test_running_it_twice_closes_each_session_once(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "quiet", minutes_ago=120)
    assert repository.close_idle_sessions(IDLE_MINUTES) == ["quiet"]
    assert repository.close_idle_sessions(IDLE_MINUTES) == []
    assert _rows(database, "SELECT count(*) FROM job") == [(1,)]


def test_an_existing_queued_job_is_not_duplicated(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "quiet", minutes_ago=120)
    _execute(database, "INSERT INTO job (job_type, session_id) VALUES ('index_session', 'quiet')")

    repository.close_idle_sessions(IDLE_MINUTES)

    assert _rows(database, "SELECT count(*) FROM job WHERE status = 'queued'") == [(1,)]


def test_an_end_reason_that_is_already_known_is_kept(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "quiet", minutes_ago=120, end_reason="prompt_input_exit")
    repository.close_idle_sessions(IDLE_MINUTES)
    assert _rows(database, "SELECT end_reason FROM session") == [("prompt_input_exit",)]


def test_several_sessions_are_closed_in_one_call_in_a_stable_order(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    for name in ("c", "a", "b"):
        _add_session(database, name, minutes_ago=90)
    assert repository.close_idle_sessions(IDLE_MINUTES) == ["a", "b", "c"]
    assert _rows(database, "SELECT count(*) FROM job") == [(3,)]


def test_a_late_chunk_reopens_a_closed_session_and_it_stays_open(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    _add_session(database, "late", minutes_ago=120)
    repository.close_idle_sessions(IDLE_MINUTES)

    repository.record_chunk("late")  # a chunk arrives after the session was closed

    assert _rows(database, "SELECT status FROM session") == [("open",)]
    assert repository.close_idle_sessions(IDLE_MINUTES) == []  # it is fresh again


def test_the_close_idle_command_closes_sessions_in_the_configured_database(
    database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _add_session(database, "quiet", minutes_ago=120)
    monkeypatch.setenv("POSTGRES_DB", database.name)  # the environment wins over .env

    assert main(["close-idle"]) == 0

    assert "Closed 1 idle session(s): quiet" in capsys.readouterr().out
    assert _rows(database, "SELECT status FROM session") == [("closed",)]


def test_a_session_that_receives_a_chunk_at_the_same_moment_is_not_closed(
    database: DatabaseSettings, repository: PostgresRepository
) -> None:
    """The closer must re-check a row that is being updated, not close it on stale information.

    An upload holds the session's row lock (its transaction is not yet committed) while the closer
    runs. The closer has to wait, and when the upload commits, see the fresh activity and leave
    the session open.
    """
    _add_session(database, "racing", minutes_ago=120)  # idle, so due to be closed

    uploader = connect(database)
    uploader.cursor().execute(
        "UPDATE session SET last_activity_at = now() WHERE session_id = 'racing'"
    )  # row is now locked and, once committed, fresh

    result: list[list[str]] = []
    closer = threading.Thread(
        target=lambda: result.append(repository.close_idle_sessions(IDLE_MINUTES))
    )
    closer.start()
    time.sleep(0.5)  # let the closer reach the locked row and wait for it
    assert closer.is_alive(), "the closer should be waiting for the uploader's lock"

    uploader.commit()  # the upload finishes
    uploader.close()
    closer.join(timeout=10)

    assert result == [[]]  # nothing was closed
    assert _rows(database, "SELECT status FROM session") == [("open",)]
    assert _rows(database, "SELECT count(*) FROM job") == [(0,)]


# ---------------------------------------------------------------- start-up schema check


def test_the_server_starts_when_every_migration_is_applied(
    database: DatabaseSettings,
) -> None:
    from ccp.ingest.server import schema_problem

    assert schema_problem(database, REAL_MIGRATIONS) is None


def test_the_server_refuses_to_start_when_migrations_are_pending(
    temp_database: DatabaseSettings,
) -> None:
    """A real mistake: the server ran against a database that was missing migration 003."""
    from ccp.db.migrations import discover_migrations, migrate
    from ccp.ingest.server import schema_problem

    migrations = discover_migrations(REAL_MIGRATIONS)
    connection = connect(temp_database)
    try:
        migrate(connection, migrations, target=2)  # 003 and later are left out
    finally:
        connection.close()

    problem = schema_problem(temp_database, REAL_MIGRATIONS)

    assert problem is not None
    assert "003_redaction_log_idempotent" in problem and "ccp-db migrate" in problem


def test_an_empty_database_is_reported_as_behind(temp_database: DatabaseSettings) -> None:
    from ccp.ingest.server import schema_problem

    problem = schema_problem(temp_database, REAL_MIGRATIONS)
    assert problem is not None and "001_initial_schema" in problem


def test_the_check_is_skipped_when_there_is_no_migrations_folder(
    temp_database: DatabaseSettings, tmp_path: Path
) -> None:
    from ccp.ingest.server import schema_problem

    assert schema_problem(temp_database, tmp_path / "nope") is None


def test_the_check_is_skipped_when_the_database_cannot_be_reached(
    temp_database: DatabaseSettings,
) -> None:
    from dataclasses import replace

    from ccp.ingest.server import schema_problem

    unreachable = replace(temp_database, port=1)
    assert schema_problem(unreachable, REAL_MIGRATIONS) is None  # /health will report it instead
