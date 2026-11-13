"""Sub-agent runs, indexed from a transcript (real PostgreSQL, see conftest.py).

The shapes follow what Claude Code 2.1.289 writes (docs/verify/transcript-facts.md, fact 17): the
parent calls the tool ``Agent``; the sub-agent has its own stream, and a one-line description
stream (``agent-<id>.meta``) names the launching call in ``toolUseId``.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from ccp.db.connection import connect
from ccp.db.migrations import discover_migrations, migrate
from ccp.index.repository import IndexRepository
from ccp.index.worker import process_next_job
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

SESSION = "sess-agents"
MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"


def _chunk(*entries: dict[str, Any]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _assistant(uuid: str, time: str, *blocks: dict[str, Any], side: bool = False) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": f"2026-07-01T10:{time}.000Z",
        "isSidechain": side,
        "message": {"content": list(blocks)},
    }


def _launch(tool_use_id: str, prompt: str, uuid: str, time: str, side: bool = False) -> bytes:
    call = {"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": {"prompt": prompt}}
    return _chunk(_assistant(uuid, time, call, side=side))


def _put(store: ObjectStore, stream: str, data: bytes, offset: int = 0) -> None:
    store.put(transcript_chunk_key(SESSION, stream, offset), data)


def _meta(**fields: Any) -> bytes:
    return (json.dumps(fields) + "\n").encode()


def _index(database: DatabaseSettings, store: ObjectStore) -> None:
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


def _one_agent_session(store: ObjectStore) -> None:
    _put(store, "main", _launch("toolu_1", "Find the config files", "m1", "00:00"))
    sub_agent = _chunk(
        {"type": "user", "uuid": "x1", "isSidechain": True, "message": {"content": "Find it"},
         "timestamp": "2026-07-01T10:00:05.000Z"},
        _assistant("x2", "00:06", {"type": "tool_use", "id": "t9", "name": "Glob"}, side=True),
        _assistant("x3", "00:09", {"type": "text", "text": "Two files."}, side=True),
    )  # fmt: skip
    _put(store, "agent-a1", sub_agent)
    meta = _meta(
        agentType="Explore", description="Config search", toolUseId="toolu_1", spawnDepth=1
    )
    _put(store, "agent-a1.meta", meta)


def test_a_sub_agent_run_is_linked_to_the_call_that_launched_it(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _one_agent_session(object_store)

    _index(database, object_store)

    rows = _rows(
        database,
        "SELECT a.agent_id, a.agent_type, a.description, a.spawn_depth, a.tool_use_id, "
        "e.entry_uuid, e.event_type, a.instructions, a.result, a.event_count, "
        "a.started_at AT TIME ZONE 'UTC', a.ended_at AT TIME ZONE 'UTC' "
        "FROM agent_run a JOIN event e ON e.event_id = a.launch_event_id",
    )
    (row,) = rows
    assert row[:10] == (
        "a1", "Explore", "Config search", 1, "toolu_1", "m1", "tool_call",
        "Find the config files", "Two files.", 3,
    )  # fmt: skip
    assert (str(row[10]), str(row[11])) == ("2026-07-01 10:00:05", "2026-07-01 10:00:09")


def test_the_description_is_not_stored_as_an_event(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _one_agent_session(object_store)

    _index(database, object_store)

    keys = _rows(
        database, "SELECT DISTINCT split_part(raw_object_key, '/', 3) FROM event ORDER BY 1"
    )
    assert keys == [("agent-a1",), ("main",)]


def test_a_run_exists_before_its_description_arrives_and_is_completed_by_it(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _put(object_store, "main", _launch("toolu_1", "Do it", "m1", "00:00"))
    _put(object_store, "agent-a1", _chunk(_assistant("x1", "00:01", {"type": "text"}, side=True)))
    _index(database, object_store)
    assert _rows(database, "SELECT agent_type, launch_event_id FROM agent_run") == [(None, None)]

    _put(object_store, "agent-a1.meta", _meta(agentType="Plan", toolUseId="toolu_1"))
    _index(database, object_store)

    rows = _rows(
        database, "SELECT agent_type, launch_event_id IS NOT NULL, instructions FROM agent_run"
    )
    assert rows == [("Plan", True, "Do it")]


def test_a_sub_agent_launched_by_another_sub_agent_points_at_its_call(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _put(object_store, "main", _launch("toolu_1", "Outer", "m1", "00:00"))
    _put(object_store, "agent-a1", _launch("toolu_2", "Inner", "x1", "00:01", side=True))
    _put(object_store, "agent-a1.meta", _meta(agentType="Outer", toolUseId="toolu_1", spawnDepth=1))
    _put(object_store, "agent-b2", _chunk(_assistant("y1", "00:02", {"type": "text"}, side=True)))
    _put(object_store, "agent-b2.meta", _meta(agentType="Inner", toolUseId="toolu_2", spawnDepth=2))

    _index(database, object_store)

    rows = _rows(
        database,
        "SELECT a.agent_id, e.entry_uuid, split_part(e.raw_object_key, '/', 3), a.spawn_depth "
        "FROM agent_run a JOIN event e ON e.event_id = a.launch_event_id ORDER BY a.agent_id",
    )
    assert rows == [("a1", "m1", "main", 1), ("b2", "x1", "agent-a1", 2)]


def test_a_description_without_a_matching_call_leaves_the_launch_empty(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _put(object_store, "main", _launch("toolu_1", "Do it", "m1", "00:00"))
    _put(object_store, "agent-a1.meta", _meta(agentType="Ghost", toolUseId="toolu_unknown"))

    _index(database, object_store)

    assert _rows(database, "SELECT agent_type, launch_event_id, event_count FROM agent_run") == [
        ("Ghost", None, 0)
    ]


def test_an_unreadable_description_does_not_stop_the_session_being_indexed(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _one_agent_session(object_store)
    _put(object_store, "agent-z9.meta", b"not json\n")

    _index(database, object_store)

    assert _rows(database, "SELECT status FROM job") == [("done",)]
    assert _rows(database, "SELECT agent_id FROM agent_run") == [("a1",)]


def test_indexing_again_changes_nothing(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _one_agent_session(object_store)
    _index(database, object_store)
    sql = "SELECT * FROM agent_run ORDER BY agent_id"
    before = _rows(database, sql)

    _index(database, object_store)

    assert _rows(database, sql) == before


def test_old_description_rows_are_removed_from_the_events_by_the_migration(
    temp_database: DatabaseSettings,
) -> None:
    """Before migration 007 a description was stored as an event; the migration removes those."""
    connection = connect(temp_database)
    try:
        migrations = discover_migrations(MIGRATIONS)
        migrate(connection, migrations, target=6)
        cursor = connection.cursor()
        cursor.execute("INSERT INTO session (session_id) VALUES ('s1')")
        for stream in ("main", "agent-a1", "agent-a1.meta"):
            cursor.execute(
                "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, "
                "raw_line_number) VALUES ('s1', 'x', 'system', '{}', %s, 1)",
                (f"sessions/s1/{stream}/000000000000.jsonl",),
            )
        connection.commit()

        migrate(connection, migrations)

        cursor.execute("SELECT split_part(raw_object_key, '/', 3) FROM event ORDER BY 1")
        assert [row[0] for row in cursor.fetchall()] == ["agent-a1", "main"]
    finally:
        connection.close()


def test_the_tool_call_ids_of_existing_events_are_filled_in_by_the_migration(
    temp_database: DatabaseSettings,
) -> None:
    connection = connect(temp_database)
    try:
        migrations = discover_migrations(MIGRATIONS)
        migrate(connection, migrations, target=8)
        cursor = connection.cursor()
        cursor.execute("INSERT INTO session (session_id) VALUES ('s1')")
        payloads = {
            1: {"message": {"content": [{"type": "tool_use", "id": "toolu_1"}, {"type": "text"}]}},
            2: {"message": {"content": "plain text"}},
            3: {"type": "cost-state"},
        }
        for line, payload in payloads.items():
            cursor.execute(
                "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, "
                "raw_line_number) VALUES ('s1', 'x', 'system', %s::jsonb, 'sessions/s1/main/0', %s)",
                (json.dumps(payload), line),
            )
        connection.commit()

        migrate(connection, migrations)

        cursor.execute("SELECT tool_use_ids FROM event ORDER BY raw_line_number")
        assert [row[0] for row in cursor.fetchall()] == [["toolu_1"], [], []]
    finally:
        connection.close()
