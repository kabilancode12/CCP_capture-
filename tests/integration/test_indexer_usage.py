"""Model usage and cost, indexed from a transcript (real PostgreSQL, see conftest.py).

The shapes follow what Claude Code 2.1.289 writes (docs/verify/transcript-facts.md, facts 12 and 13).
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

SESSION = "sess-usage"


def _chunk(*entries: dict[str, Any]) -> bytes:
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


def _assistant(
    uuid: str, message_id: str | None, block: dict[str, Any], **usage: Any
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "model": "model-a",
        "content": [block],
        "usage": {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 5,
                  "cache_creation_input_tokens": 7,
                  "output_tokens_details": {"thinking_tokens": 40}, **usage},
    }  # fmt: skip
    if message_id:
        message["id"] = message_id
    return {"type": "assistant", "uuid": uuid, "parentUuid": None, "message": message}


def _cost_state(total: float, **extra: Any) -> dict[str, Any]:
    return {
        "type": "cost-state",
        "sessionId": SESSION,
        "totalCostUSD": total,
        "totalLinesAdded": 12,
        "totalLinesRemoved": 3,
        "totalAPIDuration": 5000,
        "totalToolDuration": 300,
        "hasUnknownModelCost": False,
        "modelUsage": {
            "model-a": {
                "inputTokens": 20, "outputTokens": 200, "thinkingTokens": 80,
                "cacheReadInputTokens": 10, "cacheCreationInputTokens": 14,
                "webSearchRequests": 0, "costUSD": total - 0.01,
            },
            "helper-model": {"inputTokens": 900, "outputTokens": 10, "costUSD": 0.01},
        },
        **extra,
    }  # fmt: skip


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


def test_a_response_written_as_several_entries_is_counted_once(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    data = _chunk(
        _assistant("a1", "msg_1", {"type": "thinking", "thinking": ""}),
        _assistant("a2", "msg_1", {"type": "tool_use", "name": "Bash"}),  # same response, 2nd block
        _assistant("a3", "msg_2", {"type": "text", "text": "done"}, output_tokens=50),
    )
    _index(database, object_store, data)

    rows = _rows(
        database,
        "SELECT m.message_id, e.entry_uuid, m.model, m.input_tokens, m.output_tokens, "
        "m.thinking_tokens, m.cache_read_tokens, m.cache_creation_tokens "
        "FROM model_usage m JOIN event e USING (event_id) ORDER BY m.message_id",
    )

    assert rows == [
        ("msg_1", "a1", "model-a", 10, 100, 40, 5, 7),
        ("msg_2", "a3", "model-a", 10, 50, 40, 5, 7),
    ]


def test_an_entry_without_a_message_id_is_counted_by_its_own_uuid(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk(_assistant("a1", None, {"type": "text"})))

    assert _rows(database, "SELECT message_id FROM model_usage") == [("a1",)]


def test_entries_without_usage_are_ignored(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    entry = {"type": "assistant", "uuid": "a1", "message": {"id": "m", "content": []}}
    odd = _assistant("a2", "m2", {"type": "text"}, output_tokens="many", input_tokens=10**30)
    _index(database, object_store, _chunk(entry, odd))

    assert _rows(database, "SELECT message_id, input_tokens, output_tokens FROM model_usage") == [
        ("m2", None, None)
    ]


def test_a_repeated_response_in_a_later_chunk_is_not_counted_again(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _chunk(_assistant("a1", "msg_1", {"type": "text"}))
    _index(database, object_store, first)
    _index(database, object_store, _chunk(_assistant("a2", "msg_1", {"type": "text"})), len(first))

    assert _rows(database, "SELECT count(*) FROM model_usage") == [(1,)]


def test_the_latest_cost_state_gives_the_session_cost_and_the_cost_per_model(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk(_cost_state(0.5), _cost_state(1.25)))

    assert _rows(
        database,
        "SELECT session_id, total_cost_usd::text, has_unknown_model_cost, lines_added, "
        "lines_removed, api_duration_ms, tool_duration_ms FROM session_cost",
    ) == [(SESSION, "1.250000", False, 12, 3, 5000, 300)]
    models = _rows(
        database,
        "SELECT model, input_tokens, output_tokens, thinking_tokens, cache_read_tokens, "
        "cache_creation_tokens, web_search_requests, cost_usd::text "
        "FROM session_model_cost ORDER BY model",
    )
    assert models == [
        ("helper-model", 900, 10, None, None, None, None, "0.010000"),
        ("model-a", 20, 200, 80, 10, 14, 0, "1.240000"),
    ]


def test_a_later_cost_state_in_a_later_chunk_replaces_the_earlier_one(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _chunk(_cost_state(0.5))
    _index(database, object_store, first)
    _index(database, object_store, _chunk(_cost_state(2.0)), len(first))

    assert _rows(database, "SELECT total_cost_usd::text FROM session_cost") == [("2.000000",)]
    assert _rows(database, "SELECT count(*) FROM session_model_cost") == [(2,)]


def test_a_model_that_is_no_longer_listed_is_removed(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    first = _chunk(_cost_state(0.5))
    _index(database, object_store, first)
    later = _cost_state(0.6)
    del later["modelUsage"]["helper-model"]
    _index(database, object_store, _chunk(later), len(first))

    assert _rows(database, "SELECT model FROM session_model_cost") == [("model-a",)]


def test_a_cost_state_with_missing_or_odd_fields_is_stored_as_far_as_possible(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    odd = {"type": "cost-state", "totalCostUSD": "free", "modelUsage": "none"}
    _index(database, object_store, _chunk(odd))

    assert _rows(database, "SELECT total_cost_usd, lines_added FROM session_cost") == [(None, None)]
    assert _rows(database, "SELECT count(*) FROM session_model_cost") == [(0,)]


def test_a_session_without_cost_state_has_no_cost_rows(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(database, object_store, _chunk(_assistant("a1", "m", {"type": "text"})))

    assert _rows(database, "SELECT count(*) FROM session_cost") == [(0,)]


def test_indexing_again_changes_nothing(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    _index(
        database, object_store, _chunk(_assistant("a1", "m", {"type": "text"}), _cost_state(1.0))
    )
    sql = "SELECT 'u', message_id, output_tokens::text FROM model_usage UNION ALL " \
          "SELECT 'c', model, cost_usd::text FROM session_model_cost ORDER BY 1, 2"  # fmt: skip
    before = _rows(database, sql)

    PostgresRepository(database).record_chunk(SESSION)
    process_next_job(object_store, IndexRepository(database))

    assert _rows(database, sql) == before
