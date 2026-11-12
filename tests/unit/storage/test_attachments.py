import json
from typing import Any

import pytest

from ccp.storage import ObjectNotFoundError
from ccp.storage.attachments import (
    MIN_EXTERNAL_BYTES,
    POINTER_KEY,
    externalise_line,
    restore_line,
)

from .conftest import MemoryStore


def _compact(value: Any) -> str:
    """Serialise the way Claude Code does."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _attachment(body: Any, uuid: str = "u1", session: str = "s1", **extra: Any) -> str:
    entry = {
        "type": "attachment",
        "uuid": uuid,
        "parentUuid": "p0",
        "sessionId": session,
        "attachment": body,
        **extra,
    }
    return _compact(entry)


BIG = {"type": "skill_listing", "text": "x" * (MIN_EXTERNAL_BYTES * 2)}


def test_a_large_attachment_is_replaced_by_a_pointer_and_restored_exactly(
    memory_store: MemoryStore,
) -> None:
    raw = _attachment(BIG)
    result = externalise_line(raw, memory_store)

    assert len(result.text) < len(raw) / 5  # much smaller
    assert POINTER_KEY in json.loads(result.text)["attachment"]
    assert (result.new_objects, result.reused_objects) == (1, 0)
    assert restore_line(result.text, memory_store) == raw  # byte-for-byte


def test_the_second_copy_of_the_same_content_is_not_stored_again(memory_store: MemoryStore) -> None:
    first = externalise_line(_attachment(BIG, uuid="a", session="s1"), memory_store)
    second = externalise_line(
        _attachment(BIG, uuid="b", session="s2"), memory_store
    )  # other session

    assert (first.new_objects, second.new_objects, second.reused_objects) == (1, 0, 1)
    assert len(memory_store.objects) == 1
    # The two pointer lines differ only in their ids; both restore to their own original.
    assert restore_line(second.text, memory_store) == _attachment(BIG, uuid="b", session="s2")


def test_different_content_gets_different_objects(memory_store: MemoryStore) -> None:
    other = {"type": "skill_listing", "text": "y" * (MIN_EXTERNAL_BYTES * 2)}
    externalise_line(_attachment(BIG), memory_store)
    externalise_line(_attachment(other), memory_store)
    assert len(memory_store.objects) == 2


def test_every_large_value_of_an_attachment_entry_is_moved(memory_store: MemoryStore) -> None:
    raw = _attachment(BIG, rendered="r" * (MIN_EXTERNAL_BYTES + 1), small="tiny")
    result = externalise_line(raw, memory_store)
    entry = json.loads(result.text)
    assert POINTER_KEY in entry["attachment"] and POINTER_KEY in entry["rendered"]
    assert entry["small"] == "tiny"  # small values stay in the line
    assert result.new_objects == 2
    assert restore_line(result.text, memory_store) == raw


def test_small_attachments_are_left_alone(memory_store: MemoryStore) -> None:
    raw = _attachment({"type": "date", "date": "2026-11-12"})
    result = externalise_line(raw, memory_store)
    assert result.text == raw
    assert memory_store.objects == {}


def test_only_attachment_entries_are_touched(memory_store: MemoryStore) -> None:
    raw = _compact({"type": "user", "uuid": "u", "message": {"content": "z" * 5000}})
    assert externalise_line(raw, memory_store).text == raw
    assert memory_store.objects == {}


def test_identifying_fields_are_never_moved_out(memory_store: MemoryStore) -> None:
    raw = _attachment({"a": 1}, uuid="u" * 5000)  # absurdly long id
    result = externalise_line(raw, memory_store)
    assert result.text == raw  # nothing movable, nothing changed


@pytest.mark.parametrize(
    "line",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"type": "attachment", "attachment": {"a": "' + "x" * 3000 + '"}}',  # pretty, not compact
    ],
)
def test_lines_that_are_not_claude_code_format_are_returned_unchanged(
    line: str, memory_store: MemoryStore
) -> None:
    assert externalise_line(line, memory_store).text == line
    assert memory_store.objects == {}


def test_unicode_and_value_types_survive_the_round_trip(memory_store: MemoryStore) -> None:
    body = {
        "text": "नमस्ते 😀 " * 300,
        "numbers": [1, 2.5, -3, 0.0],
        "flags": [True, False, None],
        "nested": {"b": 1, "a": {"c": [{"d": "e"}]}},  # key order must be kept
    }
    raw = _attachment(body)
    assert restore_line(externalise_line(raw, memory_store).text, memory_store) == raw


def test_a_number_format_that_would_change_is_left_unchanged(memory_store: MemoryStore) -> None:
    # 1E5 re-serialises as 100000.0, so the line could not be rebuilt exactly: keep it as it is.
    raw = '{"type":"attachment","attachment":{"n":1E5,"t":"' + "x" * 3000 + '"}}'
    assert externalise_line(raw, memory_store).text == raw
    assert memory_store.objects == {}


def test_restore_leaves_lines_without_pointers_alone(memory_store: MemoryStore) -> None:
    for line in (_attachment({"a": 1}), "plain text", "[1]"):
        assert restore_line(line, memory_store) == line


def test_restore_fails_clearly_when_a_stored_value_is_missing(memory_store: MemoryStore) -> None:
    result = externalise_line(_attachment(BIG), memory_store)
    memory_store.objects.clear()
    with pytest.raises(ObjectNotFoundError):
        restore_line(result.text, memory_store)


def test_the_stored_bytes_are_the_compact_json_of_the_value(memory_store: MemoryStore) -> None:
    externalise_line(_attachment(BIG), memory_store)
    (stored,) = memory_store.objects.values()
    assert stored.decode("utf-8") == _compact(BIG)
