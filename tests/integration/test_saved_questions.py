"""The six saved questions (views from migration 011), answered on one invented session.

The session is indexed by the real indexer, so these tests cover the whole path from transcript
lines to answers. The story: the developer asks for a fix; the AI edits, the tests fail, it edits
again, the tests pass; a command is refused; the conversation compacts; the developer rewinds
and asks something else, which launches a sub-agent. Every expected value below can be read off
that story.
"""

import json
from datetime import UTC, datetime
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

SESSION = "sess-story"


def _at(minute: int) -> str:
    return f"2026-07-01T10:{minute:02d}:00.000Z"


def _time(minute: int) -> datetime:
    return datetime(2026, 7, 1, 10, minute, tzinfo=UTC)


def _prompt(uuid: str, parent: str | None, minute: int, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": _at(minute),
        "cwd": "/work/app",
        "message": {"content": text},
    }


def _call(
    uuid: str, parent: str, minute: int, tool: str, call_id: str, **tool_input: Any
) -> dict[str, Any]:
    block = {"type": "tool_use", "id": call_id, "name": tool, "input": tool_input}
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": _at(minute),
        "message": {"content": [block]},
    }


def _result(uuid: str, parent: str, minute: int, call_id: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": _at(minute),
        "cwd": "/work/app",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]},
    }
    entry.update(extra)
    return entry


def _edit(uuid: str, parent: str, minute: int, call_id: str) -> dict[str, Any]:
    patch = [
        {"oldStart": 3, "oldLines": 1, "newStart": 3, "newLines": 2, "lines": ["-a", "+b", "+c"]}
    ]
    return _result(
        uuid, parent, minute, call_id,
        toolUseResult={"filePath": "/work/app/src/a.py", "structuredPatch": patch, "userModified": False},
    )  # fmt: skip


def _failed(
    uuid: str, parent: str, minute: int, call_id: str, text: str, **extra: Any
) -> dict[str, Any]:
    entry = _result(uuid, parent, minute, call_id, toolUseResult=text, **extra)
    entry["message"]["content"][0].update(is_error=True, content=text)
    return entry


STORY: list[dict[str, Any]] = [
    _prompt("p1", None, 0, "Fix the bug in a.py"),
    _call("a1", "p1", 1, "Edit", "e1"),
    _edit("r1", "a1", 2, "e1"),
    _call("a2", "r1", 3, "Bash", "t1", command="pytest -q"),
    _failed("r2", "a2", 4, "t1", "Exit code 1\n2 failed"),
    _call("a3", "r2", 5, "Edit", "e2"),
    _edit("r3", "a3", 6, "e2"),
    _call("a4", "r3", 7, "Bash", "t2", command="pytest -q"),
    _result("r4", "a4", 8, "t2", toolUseResult={"stdout": "ok", "interrupted": False}),
    _call("a5", "r4", 9, "Bash", "d1", command="rm -rf build"),
    _failed("r5", "a5", 10, "d1", "The user rejected this", toolDenialKind="user-rejected"),
    {  # compaction: the boundary has no parent, only a logical one
        "type": "system", "subtype": "compact_boundary", "uuid": "b1", "parentUuid": None,
        "logicalParentUuid": "r5", "timestamp": _at(11),
        "compactMetadata": {"trigger": "auto", "preTokens": 9000, "postTokens": 1200},
    },
    {**_prompt("s1", "b1", 11, "SUMMARY"), "isCompactSummary": True},
    _prompt("p2", "s1", 12, "Add a feature"),  # rewound: replaced by p3 below
    _prompt("p3", "s1", 14, "Explore the code instead"),
    _call("a6", "p3", 15, "Agent", "ag1", prompt="Explore the code", description="Look around"),
    {
        "type": "cost-state", "sessionId": SESSION, "totalCostUSD": 1.5, "hasUnknownModelCost": False,
        "modelUsage": {
            "model-a": {"inputTokens": 100, "outputTokens": 50, "costUSD": 1.4},
            "helper-model": {"inputTokens": 900, "outputTokens": 10, "costUSD": 0.1},
        },
    },
]  # fmt: skip

SUB_AGENT: list[dict[str, Any]] = [
    {"type": "user", "uuid": "x1", "parentUuid": None, "isSidechain": True, "timestamp": _at(16),
     "message": {"content": "Explore the code"}},
    {"type": "assistant", "uuid": "x2", "parentUuid": "x1", "isSidechain": True, "timestamp": _at(17),
     "message": {"content": [{"type": "text", "text": "Found it."}]}},
]  # fmt: skip


def _chunk(entries: list[dict[str, Any]]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _index_story(
    database: DatabaseSettings, store: ObjectStore, main: list[dict[str, Any]] | None = None
) -> None:
    store.put(transcript_chunk_key(SESSION, "main", 0), _chunk(main or STORY))
    store.put(transcript_chunk_key(SESSION, "agent-a1", 0), _chunk(SUB_AGENT))
    meta = {"agentType": "Explore", "toolUseId": "ag1", "spawnDepth": 1}
    store.put(transcript_chunk_key(SESSION, "agent-a1.meta", 0), (json.dumps(meta) + "\n").encode())
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


@pytest.fixture
def story(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    _index_story(database, object_store)
    return database


def test_question_1_cost_and_models_per_session(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT session_id, total_cost_usd::text, has_unknown_model_cost, models, input_tokens, "
        "output_tokens, branch FROM v_session_cost",
    )

    assert rows == [(SESSION, "1.500000", False, ["model-a", "helper-model"], 1000, 60, None)]


def test_question_2_every_change_to_a_file_with_its_prompt(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT occurred_at, tool, change_type, actor_class, lines_added, lines_removed, "
        "is_abandoned, prompt FROM v_file_history WHERE path = 'src/a.py' ORDER BY occurred_at",
    )

    assert rows == [
        (_time(2), "Edit", "modified", "ai_edit", 2, 1, False, "Fix the bug in a.py"),
        (_time(6), "Edit", "modified", "ai_edit", 2, 1, False, "Fix the bug in a.py"),
    ]
    assert _rows(story, "SELECT count(*) FROM v_file_history WHERE path = 'other.py'") == [(0,)]


def test_question_3_sub_agents_with_their_instructions_and_result(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT agent_type, description, spawn_depth, instructions, result, event_count, "
        "parent_agent_id FROM v_agent_run",
    )

    assert rows == [("Explore", None, 1, "Explore the code", "Found it.", 2, None)]


def test_question_4_human_decisions_denials_and_rewinds(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT decision_type, occurred_at, detail, tool FROM v_human_decision ORDER BY decision_type",
    )

    assert rows == [
        ("denial", _time(10), "user-rejected", "Bash"),
        ("rewind", _time(14), "abandoned prompt", None),  # when the replacing prompt was typed
    ]


def test_question_5_where_a_session_compacted(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT occurred_at, trigger, pre_tokens, post_tokens, prompts_before, summary "
        "FROM v_compaction",
    )

    assert rows == [(_time(11), "auto", 9000, 1200, 1, "SUMMARY")]


def test_question_6_tests_that_failed_and_then_passed_after_edits(story: DatabaseSettings) -> None:
    rows = _rows(
        story,
        "SELECT framework, failed_command, failed_at, passed_at, edits_between "
        "FROM v_test_fix_cycle",
    )

    assert rows == [("pytest", "pytest -q", _time(3), _time(7), 1)]


def test_a_plan_decision_is_listed_as_approved_or_rejected(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    plan = [
        _prompt("p1", None, 0, "Plan it"),
        _call("a1", "p1", 1, "ExitPlanMode", "pl1", plan="the plan"),
        _result("r1", "a1", 2, "pl1"),
        _call("a2", "r1", 3, "ExitPlanMode", "pl2", plan="another plan"),
        _failed("r2", "a2", 4, "pl2", "The user rejected the plan"),
    ]
    _index_story(database, object_store, plan)

    rows = _rows(
        database,
        "SELECT decision_type, detail, tool FROM v_human_decision ORDER BY occurred_at",
    )

    assert rows == [
        ("plan_decision", "approved", "ExitPlanMode"),
        ("plan_decision", "rejected", "ExitPlanMode"),
    ]


def test_a_failure_that_is_never_followed_by_a_pass_is_not_a_fix_cycle(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    story_without_pass = [
        _prompt("p1", None, 0, "Run the tests"),
        _call("a1", "p1", 1, "Bash", "t1", command="pytest"),
        _failed("r1", "a1", 2, "t1", "Exit code 1"),
    ]
    _index_story(database, object_store, story_without_pass)

    assert _rows(database, "SELECT count(*) FROM v_test_fix_cycle") == [(0,)]
    assert _rows(database, "SELECT count(*) FROM test_run WHERE passed = FALSE") == [(1,)]


def test_work_on_an_abandoned_branch_is_kept_and_flagged(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    rewound = [
        _prompt("p0", None, 0, "Start"),
        _call("a0", "p0", 1, "Bash", "x0", command="ls"),
        _prompt("p1", "a0", 2, "First idea"),
        _call("a1", "p1", 3, "Edit", "e1"),
        _edit("r1", "a1", 4, "e1"),
        _prompt("p2", "a0", 6, "Second idea"),
    ]
    _index_story(database, object_store, rewound)

    rows = _rows(database, "SELECT is_abandoned, prompt FROM v_file_history")

    assert rows == [(True, "First idea")]
    assert _rows(database, "SELECT count(*) FROM v_file_history WHERE NOT is_abandoned") == [(0,)]


def test_the_views_are_empty_but_work_on_an_empty_database(database: DatabaseSettings) -> None:
    for view in (
        "v_session_cost",
        "v_file_history",
        "v_agent_run",
        "v_human_decision",
        "v_compaction",
        "v_test_fix_cycle",
    ):
        assert _rows(database, f"SELECT count(*) FROM {view}") == [(0,)]  # noqa: S608
