"""Which "user" entries the developer typed, and what depends on it (real PostgreSQL).

Later Claude Code versions write many user entries that no person typed: the output of a local
command, the body of a skill (isMeta), a background task finishing, a sub-agent handing back its
report. Only typed prompts and slash commands count as the developer's prompts: for the prompt
behind a file change, for the rewind rule and for the prompt counts of the views. The shapes follow
a real transcript (text invented).
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

SESSION = "sess-prompts"


def _user(uuid: str, parent: str | None, content: str, **extra: Any) -> dict[str, Any]:
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "cwd": "/work/app",
            "message": {"content": content}, **extra}  # fmt: skip


def _edit(call: str, result: str, parent: str, tool_use_id: str) -> list[dict[str, Any]]:
    """An AI edit: the call and its result."""
    patch = [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": ["-a", "+b"]}]
    return [
        {"type": "assistant", "uuid": call, "parentUuid": parent,
         "message": {"content": [{"type": "tool_use", "id": tool_use_id, "name": "Edit"}]}},
        {"type": "user", "uuid": result, "parentUuid": call, "cwd": "/work/app",
         "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
         "toolUseResult": {"filePath": "/work/app/a.py", "structuredPatch": patch}},
    ]  # fmt: skip


HUMAN = {"origin": {"kind": "human"}, "promptSource": "sdk"}
TASK = {"origin": {"kind": "task-notification", "producer": "session-task"}}
HANDBACK = {"origin": {"kind": "peer", "handback": True}, "isMeta": True}

TRANSCRIPT = [
    _user("p1", None, "please fix the bug", **HUMAN),
    *_edit("c1", "r1", "p1", "toolu_1"),
    _user("n1", "r1", "<task-notification>done</task-notification>", **TASK),
    *_edit("c2", "r2", "n1", "toolu_2"),
    _user("s1", "r2", "<command-name>/fix</command-name>\n<command-args></command-args>"),
    _user("m1", "s1", "<command-message>fix</command-message> (the skill's body)", isMeta=True),
    *_edit("c3", "r3", "m1", "toolu_3"),
    _user("o1", "r3", "<local-command-stdout>ok</local-command-stdout>"),
    _user("h1", "o1", "the sub-agent's report", **HANDBACK),
    *_edit("c4", "r4", "h1", "toolu_4"),
    _user("n2", "r4", "<task-notification>done</task-notification>", **TASK),
    _user("p2", "r4", "thanks, now the docs", **HUMAN),  # same parent as n2, typed later
]


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


@pytest.fixture
def indexed(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    data = "".join(json.dumps(entry) + "\n" for entry in TRANSCRIPT).encode()
    object_store.put(transcript_chunk_key(SESSION, "main", 0), data)
    PostgresRepository(database).record_chunk(SESSION)
    assert process_next_job(object_store, IndexRepository(database)) is True
    return database


def test_each_user_entry_is_told_apart(indexed: DatabaseSettings) -> None:
    rows = _rows(
        indexed,
        "SELECT entry_uuid, event_type, actor FROM event "
        "WHERE payload->>'type' = 'user' AND event_type <> 'tool_result' ORDER BY raw_line_number",
    )
    assert rows == [
        ("p1", "prompt", "human"),
        ("n1", "agent_message", "system"),
        ("s1", "slash_command", "human"),
        ("m1", "meta", "system"),
        ("o1", "command_output", "system"),
        ("h1", "agent_message", "subagent"),
        ("n2", "agent_message", "system"),
        ("p2", "prompt", "human"),
    ]


def test_an_edit_is_traced_to_the_prompt_or_command_the_developer_typed(
    indexed: DatabaseSettings,
) -> None:
    rows = _rows(
        indexed,
        "SELECT f.tool_use_id, p.entry_uuid FROM file_change f "
        "JOIN event p ON p.event_id = f.prompt_event_id ORDER BY f.tool_use_id",
    )
    # A task notification, a skill body or a sub-agent's report is not the prompt behind an edit.
    assert rows == [("toolu_1", "p1"), ("toolu_2", "p1"), ("toolu_3", "s1"), ("toolu_4", "s1")]


def test_a_message_from_an_agent_is_not_a_rewound_prompt(indexed: DatabaseSettings) -> None:
    assert _rows(indexed, "SELECT entry_uuid FROM event WHERE is_abandoned") == []


def test_a_slash_command_still_counts_as_a_prompt_for_the_rewind_rule(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    rewind = [
        _user("p1", None, "first try", **HUMAN),
        {"type": "assistant", "uuid": "a1", "parentUuid": "p1", "message": {"content": "ok"}},
        _user("p2", "a1", "rewound", **HUMAN),
        _user("s1", "a1", "<command-name>/compact</command-name>"),  # typed after the rewind
    ]
    data = "".join(json.dumps(entry) + "\n" for entry in rewind).encode()
    object_store.put(transcript_chunk_key(SESSION, "main", 0), data)
    PostgresRepository(database).record_chunk(SESSION)

    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, "SELECT entry_uuid FROM event WHERE is_abandoned") == [("p2",)]


def test_the_dashboard_counts_only_the_prompts_typed_by_the_developer(
    indexed: DatabaseSettings,
) -> None:
    rows = _rows(indexed, "SELECT session_id, prompts FROM d_session")
    assert rows == [(SESSION, 2)]  # p1 and p2: not the slash command, not the agents' messages
