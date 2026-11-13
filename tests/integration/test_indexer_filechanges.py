"""File changes, indexed from a transcript (real PostgreSQL, see conftest.py)."""

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

SESSION = "sess-files"
CWD = "/work/app"


def _chunk(*entries: dict[str, Any]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _prompt(uuid: str, parent: str | None, time: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-07-01T10:{time}.000Z",
        "cwd": CWD,
        "message": {"content": "please change the code"},
    }


def _call(uuid: str, parent: str, tool: str, call_id: str, side: bool = False) -> dict[str, Any]:
    block = {"type": "tool_use", "id": call_id, "name": tool, "input": {}}
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": side,
        "message": {"content": [block]},
    }


def _edit_result(uuid: str, parent: str, call_id: str, path: str, **extra: Any) -> dict[str, Any]:
    patch = [
        {
            "oldStart": 5,
            "oldLines": 2,
            "newStart": 5,
            "newLines": 3,
            "lines": [" a", "-b", "+c", "+d"],
        }
    ]
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "cwd": CWD,
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]},
        "toolUseResult": {
            "filePath": path,
            "oldString": "b",
            "newString": "c",
            "originalFile": "the whole old file",
            "structuredPatch": patch,
            "userModified": False,
            **extra,
        },
    }


def _index(database: DatabaseSettings, store: ObjectStore, data: bytes, offset: int = 0) -> None:
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


TRANSCRIPT = _chunk(
    _prompt("p1", None, "00:00"),
    _call("c1", "p1", "Edit", "toolu_1"),
    _edit_result("r1", "c1", "toolu_1", "/work/app/src/a.py"),
    _call("c2", "r1", "Write", "toolu_2"),
    {
        "type": "user",
        "uuid": "r2",
        "parentUuid": "c2",
        "cwd": CWD,
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2"}]},
        "toolUseResult": {
            "type": "create",
            "filePath": "/work/app/new.py",
            "content": "x\ny\n",
            "structuredPatch": [],
            "originalFile": None,
        },
    },
    _prompt("p2", "r2", "00:05"),
    _call("c3", "p2", "Edit", "toolu_3"),
    _edit_result("r3", "c3", "toolu_3", "/work/app/src/a.py", userModified=True),
    {  # a failed edit: the result is a plain string
        "type": "user",
        "uuid": "r4",
        "parentUuid": "r3",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_4"}]},
        "toolUseResult": "Error: file not found",
    },
)


def test_edits_become_file_changes_with_their_call_tool_and_prompt(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, TRANSCRIPT)

    rows = _rows(
        database,
        "SELECT f.path, f.change_type, f.tool, f.lines_added, f.lines_removed, f.user_modified, "
        "c.entry_uuid, p.entry_uuid, f.hunks::text "
        "FROM file_change f JOIN event e ON e.event_id = f.event_id "
        "LEFT JOIN event c ON c.event_id = f.call_event_id "
        "LEFT JOIN event p ON p.event_id = f.prompt_event_id "
        "ORDER BY e.raw_line_number",
    )

    assert [row[:8] for row in rows] == [
        ("src/a.py", "modified", "Edit", 2, 1, False, "c1", "p1"),
        ("new.py", "added", "Write", 2, 0, None, "c2", "p1"),
        ("src/a.py", "modified", "Edit", 2, 1, True, "c3", "p2"),
    ]
    assert json.loads(rows[0][8]) == [
        {"old_start": 5, "old_lines": 2, "new_start": 5, "new_lines": 3}
    ]
    assert json.loads(rows[1][8]) == [
        {"old_start": 0, "old_lines": 0, "new_start": 1, "new_lines": 2}
    ]


def test_the_history_of_one_file_lists_its_edits_in_order(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, TRANSCRIPT)

    rows = _rows(
        database,
        "SELECT e.entry_uuid, f.user_modified FROM file_change f JOIN event e USING (event_id) "
        "WHERE f.path = 'src/a.py' ORDER BY e.raw_object_key, e.raw_line_number",
    )

    assert rows == [("r1", False), ("r3", True)]


def test_a_failed_edit_is_not_a_file_change(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, TRANSCRIPT)

    assert _rows(database, "SELECT count(*) FROM file_change") == [(3,)]


def test_indexing_again_changes_nothing(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, TRANSCRIPT)
    sql = "SELECT * FROM file_change ORDER BY file_change_id"
    before = _rows(database, sql)

    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, sql) == before


def test_an_edit_in_a_later_chunk_is_added_and_linked(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _chunk(_prompt("p1", None, "00:00"), _call("c1", "p1", "Edit", "toolu_1"))
    _index(database, object_store, first)
    assert _rows(database, "SELECT count(*) FROM file_change") == [(0,)]

    _index(
        database,
        object_store,
        _chunk(_edit_result("r1", "c1", "toolu_1", "/work/app/a.py")),
        len(first),
    )

    assert _rows(
        database,
        "SELECT f.tool, p.entry_uuid FROM file_change f JOIN event p ON p.event_id = f.prompt_event_id",
    ) == [("Edit", "p1")]


def test_a_sub_agent_edit_is_traced_to_the_prompt_behind_the_agent(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    main = _chunk(_prompt("p1", None, "00:00"), _call("launch", "p1", "Agent", "toolu_a"))
    object_store.put(transcript_chunk_key(SESSION, "main", 0), main)
    sub_agent = _chunk(
        {"type": "user", "uuid": "s1", "parentUuid": None, "isSidechain": True,
         "message": {"content": "do it"}},
        _call("s2", "s1", "Edit", "toolu_b", side=True),
        _edit_result("s3", "s2", "toolu_b", "/work/app/deep.py"),
    )  # fmt: skip
    object_store.put(transcript_chunk_key(SESSION, "agent-a1", 0), sub_agent)
    meta = json.dumps({"agentType": "Edit", "toolUseId": "toolu_a", "spawnDepth": 1}) + "\n"
    object_store.put(transcript_chunk_key(SESSION, "agent-a1.meta", 0), meta.encode())
    PostgresRepository(database).record_chunk(SESSION)
    assert process_next_job(object_store, IndexRepository(database)) is True

    rows = _rows(
        database,
        "SELECT f.path, p.entry_uuid FROM file_change f JOIN event p ON p.event_id = f.prompt_event_id",
    )

    assert rows == [("deep.py", "p1")]


def test_the_big_old_file_is_not_needed_and_stays_only_in_the_event(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, TRANSCRIPT)

    assert _rows(
        database, "SELECT count(*) FROM event WHERE payload::text LIKE '%the whole old file%'"
    ) == [(2,)]
    assert _rows(database, "SELECT count(*) FROM file_change WHERE hunks::text LIKE '%whole%'") == [
        (0,)
    ]


def test_sessions_indexed_before_this_feature_are_caught_up(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, TRANSCRIPT)
    connection = connect(database)
    try:
        connection.cursor().execute("DELETE FROM file_change")
        connection.commit()
    finally:
        connection.close()

    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT count(*) FROM file_change") == [(3,)]
