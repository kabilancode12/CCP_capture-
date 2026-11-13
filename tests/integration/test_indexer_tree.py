"""The indexer's derived data (abandoned branches, compactions, session facts) on a real database."""

import json
from typing import Any

import pytest

from ccp.db.connection import connect
from ccp.index.repository import IndexRepository
from ccp.index.worker import process_next_job
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

SESSION = "sess-tree"


def _chunk(*entries: dict[str, Any]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _entry(kind: str, uuid: str, parent: str | None, time: str, **extra: Any) -> dict[str, Any]:
    content: Any = [{"type": "text", "text": "x"}] if kind == "assistant" else "words"
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-07-01T10:{time}.000Z",
        "message": {"content": content},
        **extra,
    }


def _index(database: DatabaseSettings, store: ObjectStore, data: bytes, offset: int) -> None:
    store.put(transcript_chunk_key(SESSION, "main", offset), data)
    PostgresRepository(database).record_chunk(SESSION)
    assert process_next_job(store, IndexRepository(database)) is True


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


REWIND = _chunk(
    _entry("user", "p1", None, "00:00"),
    _entry("assistant", "a1", "p1", "00:01"),
    _entry("user", "p2", "a1", "00:02"),  # rewound
    _entry("assistant", "a2", "p2", "00:03"),
    _entry("user", "p3", "a1", "00:04"),  # the prompt typed after the rewind
    _entry("assistant", "a3", "p3", "00:05"),
)


def test_a_rewound_branch_is_marked_abandoned(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, REWIND, 0)

    rows = _rows(database, "SELECT entry_uuid FROM event WHERE is_abandoned ORDER BY event_id")

    assert rows == [("p2",), ("a2",)]


def test_a_rewind_in_a_later_chunk_marks_the_earlier_branch_afterwards(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _chunk(*(json.loads(line) for line in REWIND.decode().splitlines()[:4]))
    _index(database, object_store, first, 0)
    assert _rows(database, "SELECT count(*) FROM event WHERE is_abandoned") == [(0,)]

    rest = _chunk(*(json.loads(line) for line in REWIND.decode().splitlines()[4:]))
    _index(database, object_store, rest, len(first))

    assert _rows(database, "SELECT entry_uuid FROM event WHERE is_abandoned ORDER BY event_id") == [
        ("p2",),
        ("a2",),
    ]


def test_indexing_again_keeps_the_same_marks(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, REWIND, 0)
    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT count(*) FROM event WHERE is_abandoned") == [(2,)]


COMPACTION = _chunk(
    _entry("user", "p1", None, "00:00", gitBranch="feature/x"),
    _entry("assistant", "a1", "p1", "00:01", gitBranch="feature/x"),
    {
        "type": "system",
        "subtype": "compact_boundary",
        "uuid": "b1",
        "parentUuid": None,
        "logicalParentUuid": "a1",
        "timestamp": "2026-07-01T10:00:02.000Z",
        "compactMetadata": {"trigger": "manual", "preTokens": 9000, "postTokens": 1200},
    },
)
SUMMARY = _chunk(
    _entry("user", "s1", "b1", "00:03", isCompactSummary=True),
    _entry("user", "p2", "s1", "00:04"),
)


def test_a_compaction_is_recorded_with_its_numbers(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, COMPACTION, 0)

    rows = _rows(
        database,
        "SELECT c.trigger, c.pre_tokens, c.post_tokens, c.summary, e.entry_uuid "
        "FROM compaction c JOIN event e USING (event_id)",
    )

    assert rows == [("manual", 9000, 1200, None, "b1")]


def test_the_summary_is_added_when_its_chunk_arrives_later(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, COMPACTION, 0)
    _index(database, object_store, SUMMARY, len(COMPACTION))

    assert _rows(database, "SELECT summary FROM compaction") == [("words",)]
    assert _rows(database, "SELECT count(*) FROM compaction") == [(1,)]
    assert _rows(database, "SELECT count(*) FROM event WHERE is_abandoned") == [(0,)]


def test_a_boundary_without_metadata_still_gets_a_row(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    bare = _chunk(
        {"type": "system", "subtype": "compact_boundary", "uuid": "b1", "parentUuid": None}
    )
    _index(database, object_store, bare, 0)

    assert _rows(database, "SELECT trigger, pre_tokens, summary FROM compaction") == [
        (None, None, None)
    ]


def test_the_session_gets_its_start_time_and_branch(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, COMPACTION, 0)

    rows = _rows(database, "SELECT started_at AT TIME ZONE 'UTC', branch, ended_at FROM session")

    assert str(rows[0][0]) == "2026-07-01 10:00:00"
    assert rows[0][1:] == ("feature/x", None)


def test_a_sub_agent_does_not_decide_the_session_start_or_branch(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, COMPACTION, 0)
    sub_agent = _chunk(
        _entry("user", "x1", None, "00:00", isSidechain=True, gitBranch="other"),
    )
    object_store.put(transcript_chunk_key(SESSION, "agent-a1", 0), sub_agent)
    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT branch FROM session") == [("feature/x",)]


def test_a_transcript_without_times_or_branch_leaves_the_session_alone(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk({"type": "user", "uuid": "p1"}), 0)

    assert _rows(database, "SELECT started_at, branch FROM session") == [(None, None)]
