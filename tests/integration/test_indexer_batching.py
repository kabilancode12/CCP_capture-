"""Storing events in batches gives exactly the rows that one insert per event gave.

The indexer used to insert one event (and one thinking row) per statement. It now sends a batch of
events as arrays in one statement. These tests store the same transcripts both ways and compare the
rebuild fingerprints (every column of every table, without the database's own ids), so any
difference in what is stored, NULLs, empty arrays and odd characters included, is caught.
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest
from tests.integration.story import SESSION, index_story

from ccp.db.connection import connect
from ccp.index.parser import ParsedEntry
from ccp.index.rebuild import RebuildRepository
from ccp.index.repository import IndexRepository, IndexRepositoryError
from ccp.index.worker import _read_chunk, index_session
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

# The statements of the indexer before batching, kept here as the reference.
_OLD_INSERT_EVENT = """
    INSERT INTO event (session_id, entry_uuid, parent_uuid, event_type, actor, occurred_at,
                       payload, raw_object_key, raw_line_number, tool_use_ids)
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
    ON CONFLICT (raw_object_key, raw_line_number)
    DO UPDATE SET session_id = EXCLUDED.session_id
    RETURNING event_id
"""
_OLD_INSERT_THINKING = """
    INSERT INTO thinking (event_id, block_index, text, is_redacted) VALUES (%s, %s, %s, %s)
    ON CONFLICT (event_id, block_index) DO NOTHING
"""


class OneByOneRepository(IndexRepository):
    """The indexer as it was: one statement per event and per thinking block."""

    def save_chunk(
        self, session_id: str, object_key: str, entries: Sequence[tuple[int, ParsedEntry]]
    ) -> None:
        connection = connect(self._settings)
        try:
            cursor = connection.cursor()
            for line_number, entry in entries:
                cursor.execute(
                    _OLD_INSERT_EVENT,
                    (
                        session_id,
                        entry.entry_uuid,
                        entry.parent_uuid,
                        entry.event_type,
                        entry.actor,
                        entry.occurred_at,
                        json.dumps(entry.payload, ensure_ascii=False),
                        object_key,
                        line_number,
                        list(entry.tool_use_ids),
                    ),
                )
                event_id = int(cursor.fetchone()[0])
                for block in entry.thinking:
                    cursor.execute(
                        _OLD_INSERT_THINKING,
                        (event_id, block.block_index, block.text, block.is_redacted),
                    )
            connection.commit()
        finally:
            connection.close()


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _odd_entry(number: int) -> dict[str, Any]:
    """Entry ``number`` of a long transcript that uses every shape the columns can take."""
    time = f"2026-07-01T{10 + number // 3600:02d}:{number // 60 % 60:02d}:{number % 60:02d}.000Z"
    text = f'line {number}: "quoted" back\\slash {{braces}}, commas, NULL, é 日本 \U0001f600 \t'
    shape = number % 8
    if shape == 0:  # a prompt, with no uuid every so often
        return {
            "type": "user",
            "uuid": None if number % 16 == 0 else f"u{number}",
            "timestamp": time,
            "message": {"content": text},
        }
    if shape == 1:  # several tool calls, one with an id full of awkward characters
        ids = [f"toolu_{number}a", f'toolu_{number}"b,c}}', f"toolu_{number}{{d}}"]
        return {
            "type": "assistant",
            "uuid": f"a{number}",
            "parentUuid": f"u{number - 1}",
            "timestamp": time,
            "message": {
                "content": [
                    {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": text}}
                    for tool_id in ids
                ]
            },
        }
    if shape == 2:  # the results, one of them without an id
        return {
            "type": "user",
            "uuid": f"r{number}",
            "parentUuid": f"a{number - 1}",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": f"toolu_{number - 1}a", "content": text},
                    {"type": "tool_result", "content": "no id"},
                ]
            },
        }
    if shape == 3:  # thinking: with text, without text, redacted
        return {
            "type": "assistant",
            "uuid": f"t{number}",
            "timestamp": "not a time",
            "message": {
                "id": f"msg_{number}",
                "content": [
                    {"type": "thinking", "thinking": text, "signature": "s"},
                    {"type": "thinking", "thinking": "", "signature": "s"},
                    {"type": "redacted_thinking", "data": "x"},
                    {"type": "text", "text": text},
                ],
            },
        }
    if shape == 4:  # a tool call with an empty id (not kept as an id)
        return {
            "type": "assistant",
            "uuid": f"e{number}",
            "message": {"content": [{"type": "tool_use", "id": "", "name": "Read"}]},
        }
    if shape == 5:
        return {
            "type": "system",
            "subtype": "informational",
            "uuid": f"s{number}",
            "content": text,
            "extra": [None, True, 1.5, {"nested": [text]}],
        }
    if shape == 6:  # a NUL character, replaced by the parser
        return {"type": "user", "uuid": f"n{number}", "message": {"content": "a\x00b"}}
    return {"type": f"future-type-{number % 3}", "value": None}


def _unreadable(number: int) -> bool:
    """Whether line ``number`` of an odd chunk is left unreadable."""
    return number % 500 == 7


def _odd_chunk(lines: int) -> bytes:
    """A long chunk of odd entries, with a few unreadable lines in between."""
    out = []
    for number in range(1, lines + 1):
        if _unreadable(number):
            out.append(b"not json")
        else:
            out.append(json.dumps(_odd_entry(number), ensure_ascii=number % 2 == 0).encode())
    return b"\n".join(out) + b"\n"


def _index_both_ways(database: DatabaseSettings, store: ObjectStore) -> tuple[Any, Any]:
    """Fingerprints of the session indexed one by one, and then in batches."""
    rebuild = RebuildRepository(database)
    index_session(store, OneByOneRepository(database), SESSION)
    one_by_one = rebuild.fingerprints(SESSION)
    rebuild.reset(SESSION)
    index_session(store, IndexRepository(database), SESSION)
    return one_by_one, rebuild.fingerprints(SESSION)


def test_the_story_is_stored_exactly_as_before(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    index_story(database, object_store)  # also creates the session

    one_by_one, batched = _index_both_ways(database, object_store)

    assert batched == one_by_one


def test_a_long_odd_transcript_over_several_batches_is_stored_exactly_as_before(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _odd_chunk(2600))
    object_store.put(transcript_chunk_key(SESSION, "agent-a1", 0), _odd_chunk(30))
    PostgresRepository(database).record_chunk(SESSION)

    one_by_one, batched = _index_both_ways(database, object_store)

    assert batched == one_by_one
    counts = _rows(
        database,
        "SELECT count(*), count(*) FILTER (WHERE cardinality(tool_use_ids) = 0), "
        "count(*) FILTER (WHERE entry_uuid IS NULL), count(*) FILTER (WHERE occurred_at IS NULL) "
        "FROM event",
    )
    readable = [n for lines in (2600, 30) for n in range(1, lines + 1) if not _unreadable(n)]
    assert counts[0][0] == len(readable)
    assert all(count > 0 for count in counts[0][1:])  # the odd shapes really are there
    assert _rows(database, "SELECT count(*) FROM thinking WHERE text IS NULL")[0][0] > 0


def test_the_databases_own_ids_still_follow_the_line_order(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    """Some derived rows take "the first event"; the order of the ids must not change."""
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _odd_chunk(2600))
    PostgresRepository(database).record_chunk(SESSION)

    index_session(object_store, IndexRepository(database), SESSION)

    lines = [row[0] for row in _rows(database, "SELECT raw_line_number FROM event ORDER BY 1")]
    by_id = [
        row[0] for row in _rows(database, "SELECT raw_line_number FROM event ORDER BY event_id")
    ]
    assert by_id == lines


def test_the_tool_call_ids_are_split_back_exactly(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _odd_chunk(5))
    PostgresRepository(database).record_chunk(SESSION)

    index_session(object_store, IndexRepository(database), SESSION)

    rows = _rows(database, "SELECT raw_line_number, tool_use_ids FROM event ORDER BY 1")
    assert rows == [
        (1, ["toolu_1a", 'toolu_1"b,c}', "toolu_1{d}"]),
        (2, ["toolu_1a"]),
        (3, []),
        (4, []),
        (5, []),
    ]


def test_storing_a_chunk_again_changes_nothing_and_repairs_missing_thinking_rows(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    """A repeated chunk takes the ON CONFLICT path, which must still return every event's id."""
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _odd_chunk(1200))
    PostgresRepository(database).record_chunk(SESSION)
    repository = IndexRepository(database)
    index_session(object_store, repository, SESSION)
    rebuild = RebuildRepository(database)
    before = rebuild.fingerprints(SESSION)
    ids_before = _rows(database, "SELECT event_id FROM event ORDER BY event_id")
    connection = connect(database)
    connection.cursor().execute("DELETE FROM thinking WHERE block_index = 1")
    connection.commit()
    connection.close()
    key = transcript_chunk_key(SESSION, "main", 0)
    entries, _ = _read_chunk(object_store.get(key))
    repository.save_chunk(SESSION, key, entries)

    assert rebuild.fingerprints(SESSION) == before
    assert _rows(database, "SELECT event_id FROM event ORDER BY event_id") == ids_before


def test_a_failing_batch_stores_nothing_of_the_chunk(database: DatabaseSettings) -> None:
    PostgresRepository(database).record_chunk(SESSION)
    entries = [
        (number, ParsedEntry(None, None, "prompt", "human", None, {"n": number}))
        for number in range(1, 1500)
    ]
    entries.append((1500, ParsedEntry(None, None, "prompt", "robot", None, {})))  # not allowed

    with pytest.raises(IndexRepositoryError, match="saving events failed"):
        IndexRepository(database).save_chunk(SESSION, "sessions/x/main/0.jsonl", entries)

    assert _rows(database, "SELECT count(*) FROM event") == [(0,)]
