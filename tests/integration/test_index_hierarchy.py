"""The indexer fills the chat hierarchy (real PostgreSQL and MinIO, see conftest.py).

One invented chat per test, written as raw transcript lines like Claude Code 2.1.325 writes them
(``effort``, ``requestedModel``, ``thinkingDurationMs``, ``ai-title``, ``session-owner`` ...).
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from tests.integration.story import SESSION as STORY_SESSION
from tests.integration.story import index_story

from ccp.db.connection import connect
from ccp.db.labels import set_account_label, set_project_label
from ccp.environment import EnvironmentRecord
from ccp.index.rebuild import RebuildRepository, rebuild_session
from ccp.index.repository import IndexRepository
from ccp.index.worker import index_session
from ccp.ingest.repository import PostgresRepository
from ccp.lifecycle import START, LifecycleEvent
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import tool_result_key, transcript_chunk_key

pytestmark = pytest.mark.integration

CHAT = "chat-h1"
ACCOUNT_UUID = "11111111-2222-3333-4444-555555555555"


def _at(hour: int, minute: int) -> str:
    return f"2026-07-01T{hour:02d}:{minute:02d}:00.000Z"


def _lines(*entries: dict[str, Any]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _prompt(uuid: str, parent: str | None, at: str, text: str = "Do it") -> dict[str, Any]:
    return {
        "type": "user", "uuid": uuid, "parentUuid": parent, "timestamp": at, "cwd": "/work/app",
        "message": {"content": text},
    }  # fmt: skip


def _reply(uuid: str, parent: str, at: str, message_id: str, **extra: Any) -> dict[str, Any]:
    usage = {"input_tokens": 10, "output_tokens": 20}
    return {
        "type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": at,
        "message": {"id": message_id, "model": "claude-x", "usage": usage,
                    "content": [{"type": "text", "text": "ok"}]},
        **extra,
    }  # fmt: skip


def _call(
    uuid: str, parent: str, at: str, call_id: str, tool: str, **tool_input: Any
) -> dict[str, Any]:
    block = {"type": "tool_use", "id": call_id, "name": tool, "input": tool_input}
    return {
        "type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": at,
        "message": {"content": [block]},
    }  # fmt: skip


def _result(
    uuid: str, parent: str, at: str, call_id: str, text: str, **extra: Any
) -> dict[str, Any]:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": text}
    return {
        "type": "user", "uuid": uuid, "parentUuid": parent, "timestamp": at,
        "message": {"content": [block]}, **extra,
    }  # fmt: skip


def _failed(uuid: str, parent: str, at: str, call_id: str, text: str) -> dict[str, Any]:
    entry = _result(uuid, parent, at, call_id, text, toolUseResult=text)
    entry["message"]["content"][0]["is_error"] = True
    return entry


def _index(database: DatabaseSettings, store: ObjectStore, session: str = CHAT) -> None:
    """Index the chat as the worker does: events, refresh, test outputs."""
    index_session(store, IndexRepository(database), session)


def _put(
    store: ObjectStore, entries: list[dict[str, Any]], stream: str = "main", offset: int = 0
) -> None:
    store.put(transcript_chunk_key(CHAT, stream, offset), _lines(*entries))


def _ensure_chat(database: DatabaseSettings, session: str = CHAT) -> None:
    PostgresRepository(database).record_chunk(session)


def _rows(database: DatabaseSettings, sql: str, *params: object) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql, params) if params else cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _first(database: DatabaseSettings, sql: str, *params: object) -> Any:
    return _rows(database, sql, *params)[0][0]


# ----------------------------------------------------------------------------------- identity


def test_the_environment_record_links_account_and_project_and_the_transcript_gives_the_title(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    PostgresRepository(database).record_environment(
        CHAT,
        EnvironmentRecord(
            account_uuid=ACCOUNT_UUID, account_email="dev@example.com", folder="/work/app",
            hostname="box", git_remote="https://github.com/acme/app.git",
            git_user_name="Dev", git_user_email="dev@example.com",
        ),
    )  # fmt: skip
    _put(object_store, [
        _prompt("p1", None, _at(10, 0)),
        {"type": "ai-title", "aiTitle": "First title", "sessionId": CHAT},
        _reply("a1", "p1", _at(10, 1), "m1"),
        {"type": "ai-title", "aiTitle": "Fix the login bug", "sessionId": CHAT},
    ])  # fmt: skip

    _index(database, object_store)

    row = _rows(
        database,
        "SELECT s.title, s.is_empty, a.account_uuid, p.project_key, s.git_user_name "
        "FROM session s JOIN claude_account a USING (account_id) JOIN project p USING (project_id) "
        "WHERE s.session_id = %s", CHAT,
    )[0]  # fmt: skip
    assert row == ("Fix the login bug", False, ACCOUNT_UUID, "github.com/acme/app", "Dev")


def test_a_chat_without_an_environment_record_is_linked_from_its_transcript(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [
        {"type": "session-owner", "accountUuid": ACCOUNT_UUID, "organizationUuid": "org-1", "sessionId": CHAT},
        _prompt("p1", None, _at(10, 0)),
        _reply("a1", "p1", _at(10, 1), "m1"),
    ])  # fmt: skip

    _index(database, object_store)

    row = _rows(
        database,
        "SELECT a.account_uuid, a.organisation_uuid, p.key_kind, p.name FROM session s "
        "JOIN claude_account a USING (account_id) JOIN project p USING (project_id) "
        "WHERE s.session_id = %s", CHAT,
    )[0]  # fmt: skip
    assert row == (ACCOUNT_UUID, "org-1", "folder", "app")  # the project from the first cwd


def test_the_environments_account_comes_before_the_transcripts_and_keeps_its_label(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    PostgresRepository(database).record_environment(
        CHAT, EnvironmentRecord(account_uuid=ACCOUNT_UUID, account_email="dev@example.com")
    )
    connection = connect(database)
    set_account_label(connection, "dev@example.com", "Alice")
    connection.commit()
    connection.close()
    _put(object_store, [
        {"type": "session-owner", "accountUuid": "99999999-0000-0000-0000-000000000000"},
        _prompt("p1", None, _at(10, 0)),
    ])  # fmt: skip

    _index(database, object_store)

    assert _rows(
        database,
        "SELECT a.account_uuid, a.label FROM session s JOIN claude_account a USING (account_id)",
    ) == [(ACCOUNT_UUID, "Alice")]


def test_a_chat_with_no_message_is_empty_and_one_with_a_reply_is_not(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [
        {"type": "user", "uuid": "c1", "timestamp": _at(10, 0),
         "message": {"content": "<command-name>/model</command-name><command-args>x</command-args>"}},
        {"type": "ai-title", "aiTitle": "Just a command"},
    ])  # fmt: skip
    _index(database, object_store)
    assert _rows(database, "SELECT is_empty, title FROM session") == [(True, "Just a command")]

    _put(
        object_store,
        [_prompt("p1", None, _at(10, 5)), _reply("a1", "p1", _at(10, 6), "m1")],
        offset=999,
    )
    _index(database, object_store)
    assert _first(database, "SELECT is_empty FROM session") is False


# ---------------------------------------------------------------------------------- sessions


def test_sessions_are_inferred_after_an_idle_gap_and_keep_their_ids(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [
        _prompt("p1", None, _at(9, 0)),
        _reply("a1", "p1", _at(9, 20), "m1"),
        _prompt("p2", "a1", _at(11, 0)),  # 100 minutes later
        _reply("a2", "p2", _at(11, 10), "m2"),
        {"type": "queue-operation", "timestamp": _at(23, 0)},  # bookkeeping: ignored
    ])  # fmt: skip

    _index(database, object_store)
    first = _rows(
        database,
        "SELECT chat_session_id, seq, start_source, end_reason, run_key, started_at, ended_at "
        "FROM chat_session ORDER BY seq",
    )

    assert [(r[1], r[2], r[3], r[4]) for r in first] == [
        (1, "inferred", "inferred", None),
        (2, "inferred", "inferred", None),
    ]
    assert first[0][5] == datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    assert first[0][6] == datetime(2026, 7, 1, 9, 20, tzinfo=UTC)
    assert first[1][5] == datetime(2026, 7, 1, 11, 0, tzinfo=UTC)

    _index(database, object_store)  # again: nothing new, the same rows
    assert _rows(database, "SELECT chat_session_id FROM chat_session ORDER BY seq") == [
        (first[0][0],), (first[1][0],),
    ]  # fmt: skip


def test_the_idle_limit_can_be_set(database: DatabaseSettings, object_store: ObjectStore) -> None:
    _ensure_chat(database)
    _put(object_store, [_prompt("p1", None, _at(9, 0)), _reply("a1", "p1", _at(9, 20), "m1")])

    index_session(
        object_store, IndexRepository(database), CHAT
    )  # 20 minutes is under the 30 default
    assert _first(database, "SELECT count(*) FROM chat_session") == 1

    index_session(object_store, IndexRepository(database, idle_minutes=10), CHAT)
    assert _first(database, "SELECT count(*) FROM chat_session") == 2


def test_client_sessions_replace_inferred_ones_and_survive_a_rebuild(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [_prompt("p1", None, _at(9, 0)), _reply("a1", "p1", _at(9, 5), "m1")])
    _index(database, object_store)
    assert _first(database, "SELECT count(*) FROM chat_session WHERE run_key IS NULL") == 1

    PostgresRepository(database).record_lifecycle(
        CHAT,
        LifecycleEvent(
            START, "run-key-0001", datetime(2026, 7, 1, 9, 0, tzinfo=UTC), source="startup"
        ),
    )
    _index(database, object_store)

    assert _rows(database, "SELECT seq, run_key, start_source FROM chat_session") == [
        (2, "run-key-0001", "startup")
    ]  # the inferred row (seq 1) is gone

    rebuild_session(
        object_store, IndexRepository(database), RebuildRepository(database), CHAT, check=True
    )
    assert _rows(database, "SELECT run_key FROM chat_session") == [("run-key-0001",)]


# ------------------------------------------------------------------------------- usage columns


def test_effort_requested_model_and_thinking_time_come_from_the_assistant_lines(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    thinking = {"type": "thinking", "thinking": "hm", "signature": "s"}
    first = _reply("a1", "p1", _at(9, 1), "m1", effort="high", requestedModel="claude-opus-5-5",
                   thinkingDurationMs=1200)  # fmt: skip
    first["message"]["content"] = [thinking]
    again = {**first}  # the same entry written twice: counted once
    second = _call("a2", "a1", _at(9, 2), "t1", "Read", file_path="x")
    second["message"].update(
        id="m1", model="claude-x", usage={"input_tokens": 10, "output_tokens": 20}
    )
    second.update(effort="high", requestedModel="claude-opus-5-5", thinkingDurationMs=300)
    second["message"]["content"].insert(0, thinking)
    plain = _reply("a3", "a2", _at(9, 3), "m2")  # no effort named
    _put(object_store, [_prompt("p1", None, _at(9, 0)), first, again, second, plain])

    _index(database, object_store)

    assert _rows(
        database,
        "SELECT message_id, effort, requested_model, thinking_ms FROM model_usage ORDER BY message_id",
    ) == [("m1", "high", "claude-opus-5-5", 1500), ("m2", None, None, None)]


def test_usage_rows_stored_before_these_columns_are_completed(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(
        object_store,
        [_prompt("p1", None, _at(9, 0)), _reply("a1", "p1", _at(9, 1), "m1", effort="low")],
    )
    _index(database, object_store)
    connection = connect(database)
    connection.cursor().execute("UPDATE model_usage SET effort = NULL")
    connection.commit()
    connection.close()

    _index(database, object_store)  # nothing new to read, the refresh still fills the column

    assert _first(database, "SELECT effort FROM model_usage") == "low"


# ----------------------------------------------------------------------------------- workflows


def test_workflow_agents_are_found_by_their_description_or_by_the_launching_call(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    launch = _result(
        "r1", "a1", _at(9, 1), "wf1", "Workflow started",
        toolUseResult={"status": "async_launched", "runId": "wf_run_1", "workflowName": "build-stage"},
    )  # fmt: skip
    _put(object_store, [
        _prompt("p1", None, _at(9, 0)),
        _call("a1", "p1", _at(9, 0), "wf1", "Workflow", script="x"),
        launch,
        {"type": "user", "uuid": "c1", "timestamp": _at(9, 30),
         "message": {"content": "<command-name>/model</command-name><command-args>opus</command-args>"}},
    ])  # fmt: skip

    def agent(agent_id: str, minute: int) -> list[dict[str, Any]]:
        step = {"isSidechain": True, "agentId": agent_id}
        return [
            {**_prompt(f"{agent_id}1", None, _at(9, minute)), **step},
            {
                **_reply(
                    f"{agent_id}2",
                    f"{agent_id}1",
                    _at(9, minute + 1),
                    f"m-{agent_id}",
                    effort="max",
                ),
                **step,
            },
            {
                **_reply(
                    f"{agent_id}3",
                    f"{agent_id}2",
                    _at(9, minute + 2),
                    f"n-{agent_id}",
                    effort="low",
                ),
                **step,
            },
        ]

    for agent_id, minute, meta in (
        ("a-new", 2, {"agentType": "workflow-subagent", "ccpWorkflowRunId": "wf_run_1"}),
        (
            "a-old",
            4,
            {"agentType": "workflow-subagent"},
        ),  # captured before the client named the run
        ("a-plain", 6, {"agentType": "general-purpose"}),
    ):
        _put(object_store, agent(agent_id, minute), stream=f"agent-{agent_id}")
        store_key = transcript_chunk_key(CHAT, f"agent-{agent_id}.meta", 0)
        object_store.put(store_key, (json.dumps(meta) + "\n").encode())

    _index(database, object_store)

    assert _rows(
        database,
        "SELECT agent_id, workflow_run_id, workflow_name, effort FROM agent_run ORDER BY agent_id",
    ) == [
        ("a-new", "wf_run_1", "build-stage", "max"),
        ("a-old", "wf_run_1", "build-stage", "max"),
        ("a-plain", None, None, "max"),
    ]
    assert _rows(
        database, "SELECT workflow_run_id, workflow_name, status, agent_count FROM v_workflow_run"
    ) == [("wf_run_1", "build-stage", "async_launched", 2)]
    assert _rows(database, "SELECT name, arguments FROM v_slash_command") == [("/model", "opus")]


# ------------------------------------------------------------------------------ file changes


def _edit_result(uuid: str, parent: str, at: str, call_id: str, path: str, lines: list[str],
                 start: int = 1, old_lines: int = 0) -> dict[str, Any]:  # fmt: skip
    patch = [{"oldStart": start, "oldLines": old_lines, "newStart": start,
              "newLines": len(lines), "lines": lines}]  # fmt: skip
    return _result(uuid, parent, at, call_id, "done",
                   toolUseResult={"filePath": path, "structuredPatch": patch, "userModified": False},
                   cwd="/work/app")  # fmt: skip


def test_file_changes_get_a_role_and_count_the_tests_they_add(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [
        _prompt("p1", None, _at(9, 0)),
        _edit_result("r1", "p1", _at(9, 1), "e1", "/work/app/tests/test_a.py",
                     ["+def test_a():", "+    assert True", "+async def test_b():", "+    pass"]),
        _edit_result("r2", "r1", _at(9, 2), "e2", "/work/app/src/a.py", ["+def helper():", "+    pass"]),
        _edit_result("r3", "r2", _at(9, 3), "e3", "/work/app/README.md", ["+# Title"]),
        _edit_result("r4", "r3", _at(9, 4), "e4", "/work/app/pyproject.toml", ["+x = 1"]),
        _edit_result("r5", "r4", _at(9, 5), "e5", "/work/app/data.bin", ["+x"]),
    ])  # fmt: skip

    _index(database, object_store)

    assert _rows(
        database, 'SELECT path, file_role, tests_added FROM file_change ORDER BY path COLLATE "C"'
    ) == [
        ("README.md", "docs", 0),
        ("data.bin", "other", 0),
        ("pyproject.toml", "config", 0),
        ("src/a.py", "code", 0),
        ("tests/test_a.py", "test", 2),
    ]


def test_file_changes_stored_before_roles_existed_are_completed(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [
        _prompt("p1", None, _at(9, 0)),
        _edit_result("r1", "p1", _at(9, 1), "e1", "/work/app/tests/test_a.py", ["+def test_a():"]),
    ])  # fmt: skip
    _index(database, object_store)
    connection = connect(database)
    connection.cursor().execute("UPDATE file_change SET file_role = NULL, tests_added = NULL")
    connection.commit()
    connection.close()

    _index(database, object_store)

    assert _rows(database, "SELECT file_role, tests_added FROM file_change") == [("test", 1)]


# --------------------------------------------------------------------------------- test runs

PYTEST_OUTPUT = (
    "Exit code 1\n=== short test summary info ===\n"
    "FAILED tests/test_a.py::test_login - assert 401 == 200\n"
    "=== 1 failed, 4 passed, 1 skipped in 0.5s ===\n"
)


def _test_chat(output_call: str, output_text: str, failed: bool = True) -> list[dict[str, Any]]:
    result = (
        _failed("r1", "a1", _at(9, 2), "t1", output_text)
        if failed
        else _result(
            "r1", "a1", _at(9, 2), "t1", output_text, toolUseResult={"stdout": output_text}
        )
    )
    return [
        _prompt("p1", None, _at(9, 0)),
        _call("a1", "p1", _at(9, 1), "t1", "Bash", command=output_call),
        result,
    ]


def test_an_inline_test_output_gives_counts_and_test_cases(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, _test_chat("pytest -q", PYTEST_OUTPUT))

    _index(database, object_store)

    assert _rows(
        database,
        "SELECT framework, tests_passed, tests_failed, tests_skipped, output_source FROM test_run",
    ) == [("pytest", 4, 1, 1, "inline")]
    assert _rows(database, "SELECT position, name, outcome, message FROM test_case") == [
        (1, "tests/test_a.py::test_login", "failed", "assert 401 == 200")
    ]


def test_a_test_output_in_an_uploaded_file_is_read_once_the_file_has_arrived(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    note = (
        "<persisted-output>\nOutput too large (31 KB). Full output saved to: "
        "/home/dev/.claude/projects/-work-app/chat-h1/tool-results/bx12.txt\n\nPreview:\n..."
        "</persisted-output>"
    )
    _put(object_store, _test_chat("pytest", note, failed=False))

    _index(database, object_store)
    assert _rows(database, "SELECT tests_passed, output_source FROM test_run") == [
        (None, "missing")
    ]
    assert _first(database, "SELECT count(*) FROM test_case") == 0

    object_store.put(
        tool_result_key(CHAT, "bx12.txt"),
        b"FAILED tests/test_b.py::test_x - boom\n== 2 failed, 9 passed in 1s ==\n",
    )
    _index(database, object_store)  # the next refresh finds the file

    assert _rows(database, "SELECT tests_passed, tests_failed, output_source FROM test_run") == [
        (9, 2, "tool_result_file")
    ]
    assert _rows(database, "SELECT name, outcome FROM test_case") == [
        ("tests/test_b.py::test_x", "failed")
    ]


def test_a_runner_without_a_parser_has_no_counts_but_a_source(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, _test_chat("mvn -q test", "BUILD SUCCESS", failed=False))

    _index(database, object_store)

    assert _rows(database, "SELECT tests_passed, output_source FROM test_run") == [(None, "inline")]


def test_at_most_500_test_cases_with_short_messages_are_kept(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    lines = [f"tests/t.py::test_{i} PASSED" for i in range(520)]
    lines += ["FAILED tests/t.py::test_bad - " + "x" * 900, "=== 1 failed, 520 passed in 9s ==="]
    _put(object_store, _test_chat("pytest -v", "\n".join(lines), failed=False))

    _index(database, object_store)

    assert _first(database, "SELECT count(*) FROM test_case") == 500
    assert _rows(
        database, "SELECT position, name, length(message) FROM test_case ORDER BY position LIMIT 1"
    ) == [(1, "tests/t.py::test_bad", 300)]
    assert _first(database, "SELECT tests_passed FROM test_run") == 520


# ---------------------------------------------------------------------------------- corrections


def test_the_story_has_every_kind_of_correction_by_rule(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    index_story(database, object_store)

    rows = _rows(
        database,
        "SELECT kind, corrected_by, detected_by, cs.seq FROM correction k "
        "LEFT JOIN chat_session cs USING (chat_session_id) WHERE k.session_id = %s ORDER BY kind",
        STORY_SESSION,
    )
    kinds = {row[0]: row for row in rows}
    assert set(kinds) == {"command_retry", "refused", "rewind", "rework", "test_fix"}
    assert {k: (r[1], r[2]) for k, r in kinds.items()} == {
        "command_retry": ("claude", "rule"),
        "refused": ("developer", "rule"),
        "rewind": ("developer", "rule"),
        "rework": ("claude", "rule"),
        "test_fix": ("test", "rule"),
    }
    assert all(row[3] == 1 for row in rows)  # every correction is in the chat's one session
    # The pointers: the failed run and the passing run; the abandoned prompt and its replacement.
    pointers = _rows(
        database,
        "SELECT k.kind, t.payload ->> 'uuid', f.payload ->> 'uuid' FROM correction k "
        "JOIN event t ON t.event_id = k.trigger_event_id LEFT JOIN event f ON f.event_id = k.fix_event_id "
        "ORDER BY k.kind",
    )
    assert pointers == [
        ("command_retry", "r2", "a4"),  # pytest failed, run again two calls later
        ("refused", "r5", None),
        ("rewind", "p2", "p3"),
        ("rework", "r1", "r3"),  # the second edit redid the lines of the first
        ("test_fix", "a2", "a4"),
    ]


def test_ai_corrections_are_not_removed_by_a_refresh(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    index_story(database, object_store)
    connection = connect(database)
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO correction (session_id, kind, corrected_by, trigger_event_id, detected_by) "
        "SELECT session_id, 'developer_correction', 'developer', event_id, 'ai' FROM event "
        "WHERE entry_uuid = 'p1'"
    )
    connection.commit()
    connection.close()

    IndexRepository(database).refresh_session(STORY_SESSION)

    assert _first(database, "SELECT count(*) FROM correction WHERE detected_by = 'ai'") == 1
    assert _first(database, "SELECT count(*) FROM correction WHERE detected_by = 'rule'") == 5


def test_a_failed_command_that_is_not_run_again_is_no_retry(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    _put(object_store, [
        _prompt("p1", None, _at(9, 0)),
        _call("a1", "p1", _at(9, 1), "c1", "Bash", command="make build"),
        _failed("r1", "a1", _at(9, 2), "c1", "Exit code 2\nboom"),
        _call("a2", "r1", _at(9, 3), "c2", "Bash", command="ls"),
        _result("r2", "a2", _at(9, 4), "c2", "x", toolUseResult={"stdout": "x"}),
    ])  # fmt: skip

    _index(database, object_store)

    assert _first(database, "SELECT count(*) FROM correction") == 0


# ---------------------------------------------------------------------------------- rebuilding


def test_a_rebuild_is_identical_twice_and_never_deletes_what_is_not_index_data(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    index_story(database, object_store)
    PostgresRepository(database).record_environment(
        STORY_SESSION,
        EnvironmentRecord(
            account_uuid=ACCOUNT_UUID, account_email="dev@example.com", folder="/work/app"
        ),
    )
    PostgresRepository(database).record_lifecycle(
        STORY_SESSION,
        LifecycleEvent(
            START, "run-key-0002", datetime(2026, 7, 1, 10, 0, tzinfo=UTC), source="startup"
        ),
    )
    connection = connect(database)
    set_account_label(connection, "dev@example.com", "Alice")
    set_project_label(connection, "/work/app", "App")
    cursor = connection.cursor()
    cursor.execute(
        "INSERT INTO decision (session_id, seq, title, status, reviewed_by_kind) "
        "VALUES (%s, 1, 'Use pytest', 'approved', 'human')", (STORY_SESSION,),
    )  # fmt: skip
    cursor.execute("INSERT INTO ui_user (email, role) VALUES ('boss@example.com', 'admin')")
    cursor.execute(
        "INSERT INTO ui_access_log (user_id, action, target) "
        "SELECT user_id, 'view_chat', %s FROM ui_user", (STORY_SESSION,),
    )  # fmt: skip
    connection.commit()
    connection.close()
    # Run the indexing once more so the account, the label and the client session are all in place.
    _index(database, object_store, STORY_SESSION)

    counts = "SELECT (SELECT count(*) FROM claude_account), (SELECT count(*) FROM project), "
    counts += "(SELECT count(*) FROM decision), (SELECT count(*) FROM ui_user), "
    counts += "(SELECT count(*) FROM ui_access_log), "
    counts += "(SELECT count(*) FROM chat_session WHERE run_key IS NOT NULL), "
    counts += "(SELECT count(label) FROM claude_account), (SELECT count(label) FROM project)"
    before = _rows(database, counts)
    repository, rebuild = IndexRepository(database), RebuildRepository(database)

    first = rebuild_session(object_store, repository, rebuild, STORY_SESSION, check=True)
    second = rebuild_session(object_store, repository, rebuild, STORY_SESSION, check=True)

    assert first is not None and second is not None
    assert second.differences == ()
    assert first.differences == ()  # the first rebuild of an up to date index is identical too
    assert _rows(database, counts) == before == [(1, 1, 1, 1, 1, 1, 1, 1)]


def test_the_fingerprints_cover_the_new_tables_and_columns() -> None:
    from ccp.index.rebuild import _FINGERPRINTS

    assert {"test_case", "correction", "chat_session"} <= set(_FINGERPRINTS)
    assert "m.effort" in _FINGERPRINTS["model_usage"]
    assert "a.workflow_run_id" in _FINGERPRINTS["agent_run"]
    assert "f.file_role" in _FINGERPRINTS["file_change"]
    assert "t.tests_passed" in _FINGERPRINTS["test_run"]
    assert "s.title" in _FINGERPRINTS["session"]


def test_a_rebuild_of_a_changed_index_is_reported_as_different(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    index_story(database, object_store)
    connection = connect(database)
    connection.cursor().execute("UPDATE file_change SET file_role = 'other'")
    connection.cursor().execute("DELETE FROM correction")
    connection.commit()
    connection.close()

    result = rebuild_session(
        object_store,
        IndexRepository(database),
        RebuildRepository(database),
        STORY_SESSION,
        check=True,
    )

    assert result is not None
    assert set(result.differences) == {"file_change", "correction"}


def test_the_chat_view_follows_the_indexer_links(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)
    PostgresRepository(database).record_environment(
        CHAT, EnvironmentRecord(account_uuid=ACCOUNT_UUID, folder="/work/app", hostname="box")
    )
    _put(object_store, [
        _prompt("p1", None, _at(9, 0)),
        {"type": "ai-title", "aiTitle": "Named chat"},
        _reply("a1", "p1", _at(9, 1), "m1", effort="medium"),
    ])  # fmt: skip

    _index(database, object_store)

    assert _rows(
        database,
        "SELECT title, project_name, session_count, is_empty FROM chat WHERE chat_id = %s",
        CHAT,
    ) == [("Named chat", "app", 1, False)]
    assert _rows(database, "SELECT effort, sessions, prompts FROM d_chat") == [("medium", 1, 1)]


def test_a_chat_with_no_transcript_events_keeps_its_empty_flag(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _ensure_chat(database)

    index_session(object_store, IndexRepository(database), CHAT)

    assert _first(database, "SELECT is_empty FROM session") is False
