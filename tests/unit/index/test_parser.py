"""Reading transcript lines: what each kind of entry becomes."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ccp.index.parser import ParseError, ThinkingBlock, parse_line

FIXTURE = Path(__file__).parents[2] / "fixtures" / "synthetic_session.jsonl"


def _parse(**entry: Any) -> Any:
    return parse_line(json.dumps(entry))


def _message(*blocks: dict[str, Any], **entry: Any) -> Any:
    return _parse(message={"content": list(blocks)}, **entry)


@pytest.mark.parametrize(
    ("entry", "event_type", "actor"),
    [
        ({"type": "user", "message": {"content": "hello"}}, "prompt", "human"),
        (
            {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
            "prompt",
            "human",
        ),
        (
            {"type": "user", "isSidechain": True, "message": {"content": "do the task"}},
            "prompt",
            "subagent",
        ),
        (
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            "tool_result",
            "system",
        ),
        (
            {"type": "user", "isCompactSummary": True, "message": {"content": "s"}},
            "compaction_summary",
            "system",
        ),
        (
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            "tool_call",
            "ai",
        ),
        (
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
            "response",
            "ai",
        ),
        (
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
            "thinking",
            "ai",
        ),
        (
            {"type": "assistant", "isSidechain": True, "message": {"content": [{"type": "text"}]}},
            "response",
            "subagent",
        ),
        ({"type": "system", "subtype": "compact_boundary"}, "compact_boundary", "system"),
        ({"type": "system"}, "system", "system"),
        ({"type": "cost-state"}, "cost-state", "system"),
        ({"type": "a-type-from-a-future-version"}, "a-type-from-a-future-version", "system"),
        ({}, "unknown", "system"),
    ],
)
def test_each_kind_of_entry_gets_an_event_type_and_actor(
    entry: dict[str, Any], event_type: str, actor: str
) -> None:
    parsed = parse_line(json.dumps(entry))
    assert (parsed.event_type, parsed.actor) == (event_type, actor)


def test_an_assistant_entry_with_text_and_a_tool_call_is_a_tool_call() -> None:
    parsed = _message(
        {"type": "text", "text": "I will run it"}, {"type": "tool_use"}, type="assistant"
    )
    assert parsed.event_type == "tool_call"


def test_an_assistant_entry_with_thinking_and_text_is_a_response() -> None:
    parsed = _message({"type": "thinking", "thinking": "hm"}, {"type": "text"}, type="assistant")
    assert parsed.event_type == "response"


def test_identity_and_time_are_read() -> None:
    parsed = _parse(type="user", uuid="u2", parentUuid="u1", timestamp="2026-07-01T10:00:00.250Z")
    assert (parsed.entry_uuid, parsed.parent_uuid) == ("u2", "u1")
    assert parsed.occurred_at == datetime(2026, 7, 1, 10, 0, 0, 250000, tzinfo=UTC)


@pytest.mark.parametrize("timestamp", [None, "", "yesterday", 12345, "2026-07-01T10:00:00"])
def test_a_missing_or_unusable_time_is_none(timestamp: object) -> None:
    assert _parse(type="user", timestamp=timestamp).occurred_at is None


def test_a_first_entry_has_no_parent() -> None:
    assert _parse(type="user", uuid="u1", parentUuid=None).parent_uuid is None


def test_the_payload_is_the_whole_entry() -> None:
    entry = {"type": "user", "uuid": "u1", "message": {"content": "hi"}, "extra": [1, {"a": None}]}
    assert parse_line(json.dumps(entry)).payload == entry


def test_null_characters_are_replaced_because_postgresql_cannot_store_them() -> None:
    parsed = _parse(type="user", message={"content": "a\x00b"}, **{"k\x00": ["x\x00"]})
    assert "\x00" not in json.dumps(parsed.payload, ensure_ascii=False)
    assert parsed.payload["message"]["content"] == "a�b"


def test_thinking_text_is_kept_when_there_is() -> None:
    parsed = _message(
        {"type": "thinking", "thinking": "Plan it.", "signature": "s"}, type="assistant"
    )
    assert parsed.thinking == (ThinkingBlock(0, "Plan it.", False),)


def test_a_signature_only_thinking_block_is_recorded_without_text() -> None:
    parsed = _message(
        {"type": "text"}, {"type": "thinking", "thinking": "", "signature": "s"}, type="assistant"
    )
    assert parsed.thinking == (ThinkingBlock(1, None, False),)


def test_a_redacted_thinking_block_is_flagged() -> None:
    parsed = _message({"type": "redacted_thinking", "data": "x"}, type="assistant")
    assert parsed.thinking == (ThinkingBlock(0, None, True),)


def test_thinking_is_only_read_from_assistant_entries() -> None:
    parsed = _message({"type": "thinking", "thinking": "x"}, type="user")
    assert parsed.thinking == ()


@pytest.mark.parametrize("line", ["", "not json", "[1, 2]", '"text"', "42", "null"])
def test_a_line_that_is_not_a_json_object_is_an_error_that_does_not_quote_it(line: str) -> None:
    with pytest.raises(ParseError) as caught:
        parse_line(line)
    assert line.strip() == "" or line not in str(caught.value)


@pytest.mark.parametrize("content", [None, "text", 5, [None, "x", 3]])
def test_odd_message_content_does_not_break_reading(content: object) -> None:
    parsed = _parse(type="assistant", message={"content": content})
    assert parsed.event_type == "response"


def test_the_synthetic_session_reads_line_by_line() -> None:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    kinds = []
    for line in lines:
        try:
            kinds.append(parse_line(line).event_type)
        except ParseError:
            kinds.append("<unreadable>")
    assert kinds == [
        "summary",
        "prompt",
        "tool_call",
        "tool_result",
        "response",
        "response",
        "compact_boundary",
        "<unreadable>",
    ]


def test_the_ids_of_tool_calls_are_collected_from_assistant_entries() -> None:
    parsed = _message(
        {"type": "text"},
        {"type": "tool_use", "id": "toolu_1"},
        {"type": "tool_use", "id": "toolu_2"},
        {"type": "tool_use", "id": ""},
        {"type": "tool_use"},
        type="assistant",
    )
    assert parsed.tool_use_ids == ("toolu_1", "toolu_2")


def test_the_ids_that_tool_results_answer_are_collected_too() -> None:
    parsed = _message(
        {"type": "tool_result", "tool_use_id": "toolu_1", "id": "ignored"},
        {"type": "tool_result"},
        type="user",
    )
    assert parsed.tool_use_ids == ("toolu_1",)
