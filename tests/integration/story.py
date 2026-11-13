"""One invented session that exercises every part of the indexer, shared by several tests.

The story: the developer asks for a fix; the AI edits, the tests fail, it edits again, the tests
pass; a command is refused; the conversation compacts; the developer rewinds and asks something
else, which launches a sub-agent that replies; the transcript ends with a cost entry. Every answer
the indexer should give can be read off that story. It is indexed by the real indexer.
"""

import json
from datetime import UTC, datetime  # noqa: F401  (datetime is used by at_minute's callers)
from typing import Any

import pytest  # noqa: F401

from ccp.index.repository import IndexRepository
from ccp.index.worker import process_next_job
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

SESSION = "sess-story"


def at_minute(minute: int) -> str:
    return f"2026-07-01T10:{minute:02d}:00.000Z"


def make_prompt(uuid: str, parent: str | None, minute: int, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": at_minute(minute),
        "cwd": "/work/app",
        "message": {"content": text},
    }


def make_call(
    uuid: str, parent: str, minute: int, tool: str, call_id: str, **tool_input: Any
) -> dict[str, Any]:
    block = {"type": "tool_use", "id": call_id, "name": tool, "input": tool_input}
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": at_minute(minute),
        "message": {"content": [block]},
    }


def make_result(uuid: str, parent: str, minute: int, call_id: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": at_minute(minute),
        "cwd": "/work/app",
        "message": {"content": [{"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]},
    }
    entry.update(extra)
    return entry


def make_edit(uuid: str, parent: str, minute: int, call_id: str) -> dict[str, Any]:
    patch = [
        {"oldStart": 3, "oldLines": 1, "newStart": 3, "newLines": 2, "lines": ["-a", "+b", "+c"]}
    ]
    return make_result(
        uuid, parent, minute, call_id,
        toolUseResult={"filePath": "/work/app/src/a.py", "structuredPatch": patch, "userModified": False},
    )  # fmt: skip


def make_reply(uuid: str, parent: str, minute: int, text: str, message_id: str) -> dict[str, Any]:
    """A model reply with a thinking block (text kept) and token usage."""
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5}
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": at_minute(minute),
        "message": {
            "id": message_id,
            "model": "model-a",
            "usage": usage,
            "content": [
                {"type": "thinking", "thinking": "Let me check.", "signature": "s"},
                {"type": "text", "text": text},
            ],
        },
    }


def make_failed(
    uuid: str, parent: str, minute: int, call_id: str, text: str, **extra: Any
) -> dict[str, Any]:
    entry = make_result(uuid, parent, minute, call_id, toolUseResult=text, **extra)
    entry["message"]["content"][0].update(is_error=True, content=text)
    return entry


STORY: list[dict[str, Any]] = [
    make_prompt("p1", None, 0, "Fix the bug in a.py"),
    make_call("a1", "p1", 1, "Edit", "e1"),
    make_edit("r1", "a1", 2, "e1"),
    make_call("a2", "r1", 3, "Bash", "t1", command="pytest -q"),
    make_failed("r2", "a2", 4, "t1", "Exit code 1\n2 failed"),
    make_call("a3", "r2", 5, "Edit", "e2"),
    make_edit("r3", "a3", 6, "e2"),
    make_call("a4", "r3", 7, "Bash", "t2", command="pytest -q"),
    make_result("r4", "a4", 8, "t2", toolUseResult={"stdout": "ok", "interrupted": False}),
    make_reply("a4b", "r4", 9, "Tests pass now.", message_id="msg_1"),
    make_call("a5", "a4b", 9, "Bash", "d1", command="rm -rf build"),
    make_failed("r5", "a5", 10, "d1", "The user rejected this", toolDenialKind="user-rejected"),
    {  # compaction: the boundary has no parent, only a logical one
        "type": "system", "subtype": "compact_boundary", "uuid": "b1", "parentUuid": None,
        "logicalParentUuid": "r5", "timestamp": at_minute(11),
        "compactMetadata": {"trigger": "auto", "preTokens": 9000, "postTokens": 1200},
    },
    {**make_prompt("s1", "b1", 11, "SUMMARY"), "isCompactSummary": True},
    make_prompt("p2", "s1", 12, "Add a feature"),  # rewound: replaced by p3 below
    make_prompt("p3", "s1", 14, "Explore the code instead"),
    make_call("a6", "p3", 15, "Agent", "ag1", prompt="Explore the code", description="Look around"),
    {
        "type": "cost-state", "sessionId": SESSION, "totalCostUSD": 1.5, "hasUnknownModelCost": False,
        "modelUsage": {
            "model-a": {"inputTokens": 100, "outputTokens": 50, "costUSD": 1.4},
            "helper-model": {"inputTokens": 900, "outputTokens": 10, "costUSD": 0.1},
        },
    },
]  # fmt: skip

SUB_AGENT: list[dict[str, Any]] = [
    {"type": "user", "uuid": "x1", "parentUuid": None, "isSidechain": True, "timestamp": at_minute(16),
     "message": {"content": "Explore the code"}},
    {"type": "assistant", "uuid": "x2", "parentUuid": "x1", "isSidechain": True, "timestamp": at_minute(17),
     "message": {"content": [{"type": "text", "text": "Found it."}]}},
]  # fmt: skip


def as_chunk(entries: list[dict[str, Any]]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def index_story(
    database: DatabaseSettings, store: ObjectStore, main: list[dict[str, Any]] | None = None
) -> None:
    store.put(transcript_chunk_key(SESSION, "main", 0), as_chunk(main or STORY))
    store.put(transcript_chunk_key(SESSION, "agent-a1", 0), as_chunk(SUB_AGENT))
    meta = {"agentType": "Explore", "toolUseId": "ag1", "spawnDepth": 1}
    store.put(transcript_chunk_key(SESSION, "agent-a1.meta", 0), (json.dumps(meta) + "\n").encode())
    PostgresRepository(database).record_chunk(SESSION)
    assert process_next_job(store, IndexRepository(database)) is True
