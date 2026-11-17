"""Test runs, indexed from a transcript (real PostgreSQL, see conftest.py).

Shapes follow Claude Code 2.1.289: a successful shell call has a ``toolUseResult`` object and no
error; a failed one (non-zero exit) has ``is_error`` and text starting ``Exit code <n>``; a refused
call has ``toolDenialKind``.
"""

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

SESSION = "sess-tests"


def _chunk(*entries: dict[str, Any]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _call(call_id: str, command: str, tool: str = "Bash") -> dict[str, Any]:
    block = {"type": "tool_use", "id": call_id, "name": tool, "input": {"command": command}}
    return {"type": "assistant", "uuid": f"c-{call_id}", "message": {"content": [block]}}


def _ok(call_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": f"r-{call_id}",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]},
        "toolUseResult": {"stdout": "ok", "stderr": "", "interrupted": False},
        **extra,
    }


def _failed(call_id: str, text: str = "Exit code 1\n3 failed") -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": call_id, "is_error": True, "content": text}
    return {
        "type": "user",
        "uuid": f"r-{call_id}",
        "message": {"content": [block]},
        "toolUseResult": text,
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


RUNS = "SELECT command, framework, passed, exit_code FROM test_run ORDER BY call_event_id"


def test_a_passing_and_a_failing_run_are_recorded(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    data = _chunk(
        _call("t1", "pytest -q"),
        _ok("t1"),
        _call("t2", "cd app && npm test", tool="PowerShell"),
        _failed("t2", "Exit code 2\nnpm ERR!"),
    )
    _index(database, object_store, data)

    assert _rows(database, RUNS) == [
        ("pytest -q", "pytest", True, 0),
        ("cd app && npm test", "npm test", False, 2),
    ]


def test_commands_that_do_not_run_tests_are_ignored(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    data = _chunk(
        _call("t1", "pip install pytest"),
        _ok("t1"),
        _call("t2", "git status"),
        _ok("t2"),
        _call("t3", "pytest --collect-only"),
        _ok("t3"),
    )
    _index(database, object_store, data)

    assert _rows(database, "SELECT count(*) FROM test_run") == [(0,)]


def test_a_refused_call_never_ran_and_is_not_a_run(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    refused = _failed("t1", "The user denied this action")
    refused["toolDenialKind"] = "user"
    _index(database, object_store, _chunk(_call("t1", "pytest"), refused))

    assert _rows(database, "SELECT count(*) FROM test_run") == [(0,)]


def test_an_interrupted_run_has_no_outcome(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    interrupted = _ok("t1")
    interrupted["toolUseResult"]["interrupted"] = True
    _index(database, object_store, _chunk(_call("t1", "pytest"), interrupted))

    assert _rows(database, RUNS) == [("pytest", "pytest", None, None)]


def test_an_error_without_an_exit_code_has_no_outcome(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk(_call("t1", "pytest"), _failed("t1", "Tool crashed")))

    assert _rows(database, RUNS) == [("pytest", "pytest", None, None)]


def test_a_call_whose_result_has_not_arrived_becomes_a_run_when_it_does(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _chunk(_call("t1", "pytest"))
    _index(database, object_store, first)
    assert _rows(database, "SELECT count(*) FROM test_run") == [(0,)]

    _index(database, object_store, _chunk(_failed("t1")), len(first))

    assert _rows(database, RUNS) == [("pytest", "pytest", False, 1)]


def test_the_run_points_at_its_call_and_its_result(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk(_call("t1", "go test ./..."), _ok("t1")))

    rows = _rows(
        database,
        "SELECT c.entry_uuid, r.entry_uuid, t.tool_use_id FROM test_run t "
        "JOIN event c ON c.event_id = t.call_event_id JOIN event r ON r.event_id = t.result_event_id",
    )

    assert rows == [("c-t1", "r-t1", "t1")]


def test_a_very_long_command_is_cut(database: DatabaseSettings, object_store: ObjectStore) -> None:
    _index(database, object_store, _chunk(_call("t1", "pytest " + "x" * 5000), _ok("t1")))

    assert _rows(database, "SELECT length(command) FROM test_run") == [(2000,)]


def test_indexing_again_changes_nothing(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(
        database,
        object_store,
        _chunk(_call("t1", "pytest"), _failed("t1"), _call("t2", "ls"), _ok("t2")),
    )
    before = _rows(database, "SELECT * FROM test_run")

    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT * FROM test_run") == before


def test_a_call_is_found_from_its_result_and_back_by_tool_call_id(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk(_call("t1", "pytest"), _ok("t1")))

    rows = _rows(database, "SELECT event_type, tool_use_ids FROM event ORDER BY raw_line_number")

    assert rows == [("tool_call", ["t1"]), ("tool_result", ["t1"])]


def test_the_result_ids_of_existing_events_are_filled_in_by_the_migration(
    temp_database: DatabaseSettings,
) -> None:
    from pathlib import Path

    from ccp.db.migrations import discover_migrations, migrate

    migrations = discover_migrations(Path(__file__).parents[2] / "db" / "migrations")
    connection = connect(temp_database)
    try:
        migrate(connection, migrations, target=9)
        cursor = connection.cursor()
        cursor.execute("INSERT INTO session (session_id) VALUES ('s1')")
        results = {"message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}}
        for line, event_type in ((1, "tool_result"), (2, "prompt")):
            cursor.execute(
                "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, "
                "raw_line_number) VALUES ('s1', %s, 'system', %s::jsonb, 'sessions/s1/main/0', %s)",
                (event_type, json.dumps(results), line),
            )
        connection.commit()

        migrate(connection, migrations)

        cursor.execute("SELECT tool_use_ids FROM event ORDER BY raw_line_number")
        assert [row[0] for row in cursor.fetchall()] == [["t1"], []]
    finally:
        connection.close()
