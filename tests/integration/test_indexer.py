"""The indexer against a real PostgreSQL and MinIO (see conftest.py)."""

import json
from pathlib import Path
from typing import Any

import pytest

from ccp.db.connection import connect
from ccp.index.cli import main
from ccp.index.repository import MAX_ATTEMPTS, IndexRepository
from ccp.index.worker import process_next_job, run
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parents[1] / "fixtures" / "synthetic_session.jsonl"
SESSION = "sess-test"


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _upload(
    store: ObjectStore, database: DatabaseSettings, data: bytes, offset: int, stream: str = "main"
) -> str:
    """Store a chunk and queue the index job the way the API does."""
    key = transcript_chunk_key(SESSION, stream, offset)
    store.put(key, data)
    PostgresRepository(database).record_chunk(SESSION)
    return key


def _clean_fixture() -> bytes:
    """The synthetic session without its deliberately broken last line."""
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()[:-1]
    return ("\n".join(lines) + "\n").encode()


def test_a_session_is_indexed_into_events(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    key = _upload(object_store, database, _clean_fixture(), 0)

    assert process_next_job(object_store, IndexRepository(database)) is True

    rows = _rows(
        database,
        "SELECT raw_line_number, event_type, actor, entry_uuid, parent_uuid, raw_object_key "
        "FROM event ORDER BY raw_line_number",
    )
    assert [row[:3] for row in rows] == [
        (1, "summary", "system"),
        (2, "prompt", "human"),
        (3, "tool_call", "ai"),
        (4, "tool_result", "system"),
        (5, "response", "ai"),
        (6, "response", "subagent"),
        (7, "compact_boundary", "system"),
    ]
    assert rows[2][3:] == ("a1", "u1", key)
    assert _rows(database, "SELECT status FROM job") == [("done",)]


def test_the_payload_is_the_stored_entry(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, _clean_fixture(), 0)
    process_next_job(object_store, IndexRepository(database))

    (payload,) = _rows(database, "SELECT payload FROM event WHERE raw_line_number = 2")[0]

    assert payload["message"]["content"] == "Create hello.py"


def test_thinking_blocks_are_stored_with_their_text_when_there_is_any(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, _clean_fixture(), 0)
    process_next_job(object_store, IndexRepository(database))

    rows = _rows(
        database,
        "SELECT e.raw_line_number, t.block_index, t.text, t.is_redacted "
        "FROM thinking t JOIN event e USING (event_id) ORDER BY 1, 2",
    )

    assert rows == [(3, 0, "Plan the file.", False), (5, 0, None, False), (5, 1, None, True)]


def test_indexing_again_changes_nothing(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, _clean_fixture(), 0)
    process_next_job(object_store, IndexRepository(database))
    before = _rows(database, "SELECT event_id, event_type FROM event ORDER BY event_id")

    PostgresRepository(database).record_chunk(SESSION)  # queues another job
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT event_id, event_type FROM event ORDER BY event_id") == before
    assert _rows(database, "SELECT count(*) FROM thinking") == [(3,)]


def test_a_chunk_that_arrives_later_is_added(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _clean_fixture()
    _upload(object_store, database, first, 0)
    process_next_job(object_store, IndexRepository(database))
    second = (json.dumps({"type": "user", "uuid": "u9", "parentUuid": "a2"}) + "\n").encode()

    _upload(object_store, database, second, len(first))
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT count(*) FROM event") == [(8,)]
    assert _rows(database, "SELECT raw_line_number FROM event WHERE entry_uuid = 'u9'") == [(1,)]


def test_sub_agent_streams_belong_to_the_parent_session(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, _clean_fixture(), 0)
    data = (
        json.dumps({"type": "user", "isSidechain": True, "message": {"content": "task"}}) + "\n"
    ).encode()
    _upload(object_store, database, data, 0, stream="agent-a1")

    process_next_job(object_store, IndexRepository(database))

    rows = _rows(database, "SELECT actor FROM event WHERE raw_object_key LIKE '%agent-a1%'")
    assert rows == [("subagent",)]
    assert _rows(database, "SELECT DISTINCT session_id FROM event") == [(SESSION,)]


def test_an_unreadable_line_is_skipped_and_the_rest_is_kept(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, FIXTURE.read_bytes(), 0)  # the last line is not JSON

    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT count(*) FROM event") == [(7,)]


def test_a_failed_job_is_tried_again_and_finally_given_up(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, b"\xff\n", 0)  # not UTF-8: cannot be indexed
    repository = IndexRepository(database)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert process_next_job(object_store, repository) is True
        expected = "queued" if attempt < MAX_ATTEMPTS else "failed"
        assert _rows(database, "SELECT status, attempts FROM job") == [(expected, attempt)]

    assert process_next_job(object_store, repository) is False


def test_two_workers_never_take_the_same_job(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, _clean_fixture(), 0)

    first = IndexRepository(database).claim_job()
    second = IndexRepository(database).claim_job()

    assert first is not None
    assert second is None


def test_the_run_loop_drains_the_queue(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _upload(object_store, database, _clean_fixture(), 0)

    handled = run(object_store, IndexRepository(database), poll_seconds=0, once=True)

    assert handled == 1
    assert _rows(database, "SELECT count(*) FROM event") == [(7,)]


def test_the_command_refuses_to_run_on_an_old_schema(
    temp_database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in {
        "POSTGRES_HOST": temp_database.host,
        "POSTGRES_PORT": str(temp_database.port),
        "POSTGRES_DB": temp_database.name,
        "POSTGRES_USER": temp_database.user,
        "POSTGRES_PASSWORD": temp_database.password.reveal(),
    }.items():
        monkeypatch.setenv(name, value)

    assert (
        main(
            [
                "run",
                "--once",
                "--migrations-dir",
                str(Path(__file__).parents[2] / "db" / "migrations"),
            ]
        )
        == 1
    )
    assert "ccp-db migrate" in capsys.readouterr().err
