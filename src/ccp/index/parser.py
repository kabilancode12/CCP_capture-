"""Reading one transcript line.

A transcript is a file of JSON lines. Claude Code writes many kinds of entries and adds new ones
over time, so this module never refuses an entry because of its *kind*: an unknown type simply
becomes an event with that type name. Only a line that is not a JSON object is an error.

The module does no I/O. It turns a line of text into a :class:`ParsedEntry`, which the repository
then stores. What is known about the shapes is in docs/verify/transcript-facts.md.

How an entry becomes an event:

====================  =====================  ==========================================
Entry                 ``event_type``         ``actor``
====================  =====================  ==========================================
user, plain prompt    ``prompt``             ``human`` (``subagent`` inside a sub-agent)
user, tool results    ``tool_result``        ``system``
user, compact summary ``compaction_summary`` ``system``
assistant, tool call  ``tool_call``          ``ai`` (``subagent`` inside a sub-agent)
assistant, text       ``response``           ``ai`` (``subagent`` inside a sub-agent)
assistant, thinking   ``thinking``           ``ai`` (``subagent`` inside a sub-agent)
system                its ``subtype``        ``system``
anything else         its ``type``           ``system``
====================  =====================  ==========================================
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# An assistant entry holding both text and a tool call is a tool call: that is what it did.
_ASSISTANT_PRIORITY = ("tool_use", "text", "thinking", "redacted_thinking")
_ASSISTANT_TYPES = {
    "tool_use": "tool_call",
    "text": "response",
    "thinking": "thinking",
    "redacted_thinking": "thinking",
}


class ParseError(ValueError):
    """Raised for a line that is not a JSON object. The message never contains the line."""


@dataclass(frozen=True)
class ThinkingBlock:
    """One reasoning block of an assistant message."""

    block_index: int  # position within the message's content list
    text: str | None  # None when the transcript holds no text (only a signature)
    is_redacted: bool  # a ``redacted_thinking`` block


@dataclass(frozen=True)
class ParsedEntry:
    """What the indexer keeps of one transcript line."""

    entry_uuid: str | None
    parent_uuid: str | None
    event_type: str
    actor: str  # human | ai | subagent | system
    occurred_at: datetime | None
    payload: dict[str, Any]  # the entry itself (already redacted before upload)
    thinking: tuple[ThinkingBlock, ...] = ()
    tool_use_ids: tuple[str, ...] = ()  # ids of the tool calls in this entry


def parse_line(line: str) -> ParsedEntry:
    """Read one transcript line.

    Args:
        line: One line of a stored transcript, without its newline.

    Returns:
        The entry's identity, kind, actor, time and content, plus any thinking blocks.

    Raises:
        ParseError: If the line is not valid JSON or not a JSON object.
    """
    try:
        entry = json.loads(line)
    except json.JSONDecodeError as error:
        raise ParseError("line is not valid JSON") from error
    if not isinstance(entry, dict):
        raise ParseError("line is not a JSON object")

    kind = entry.get("type")
    kind = kind if isinstance(kind, str) and kind else "unknown"
    in_sub_agent = entry.get("isSidechain") is True
    blocks = content_blocks(entry)

    if kind == "user":
        event_type, actor = _classify_user(entry, blocks, in_sub_agent)
    elif kind == "assistant":
        event_type, actor = _classify_assistant(blocks, in_sub_agent)
    elif kind == "system":
        subtype = entry.get("subtype")
        event_type = subtype if isinstance(subtype, str) and subtype else "system"
        actor = "system"
    else:
        event_type, actor = kind, "system"

    return ParsedEntry(
        entry_uuid=_text_or_none(entry.get("uuid")),
        parent_uuid=_text_or_none(entry.get("parentUuid")),
        event_type=event_type,
        actor=actor,
        occurred_at=_parse_time(entry.get("timestamp")),
        payload=_without_null_characters(entry),
        thinking=_thinking_blocks(blocks) if kind == "assistant" else (),
        tool_use_ids=_tool_use_ids(blocks) if kind == "assistant" else (),
    )


def content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The content blocks of an entry's message; plain text or no message gives an empty list."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _classify_user(
    entry: dict[str, Any], blocks: list[dict[str, Any]], in_sub_agent: bool
) -> tuple[str, str]:
    """A user entry is a prompt, the result of tool calls, or the summary made by a compaction."""
    if entry.get("isCompactSummary") is True:
        return "compaction_summary", "system"
    if any(block.get("type") == "tool_result" for block in blocks):
        return "tool_result", "system"
    # Inside a sub-agent the "user" is the parent agent briefing it, not a person.
    return "prompt", "subagent" if in_sub_agent else "human"


def _classify_assistant(blocks: list[dict[str, Any]], in_sub_agent: bool) -> tuple[str, str]:
    """An assistant entry is named after the most telling block it holds."""
    present = {block.get("type") for block in blocks}
    event_type = "response"
    for block_type in _ASSISTANT_PRIORITY:
        if block_type in present:
            event_type = _ASSISTANT_TYPES[block_type]
            break
    return event_type, "subagent" if in_sub_agent else "ai"


def _thinking_blocks(blocks: list[dict[str, Any]]) -> tuple[ThinkingBlock, ...]:
    """The reasoning blocks of an assistant message, with their text if there is any."""
    found = []
    for index, block in enumerate(blocks):
        block_type = block.get("type")
        if block_type == "thinking":
            text = block.get("thinking")
            found.append(
                ThinkingBlock(index, text if isinstance(text, str) and text else None, False)
            )
        elif block_type == "redacted_thinking":
            found.append(ThinkingBlock(index, None, True))
    return tuple(found)


def _tool_use_ids(blocks: list[dict[str, Any]]) -> tuple[str, ...]:
    """The ids of the tool calls in an assistant message."""
    return tuple(
        block["id"]
        for block in blocks
        if block.get("type") == "tool_use" and isinstance(block.get("id"), str) and block["id"]
    )


def _text_or_none(value: object) -> str | None:
    """A non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def _parse_time(value: object) -> datetime | None:
    """Read an ISO 8601 time such as ``2026-07-01T10:00:00.000Z``; None if absent or unreadable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A time without a zone cannot be placed on a timeline reliably, and PostgreSQL would guess.
    return parsed if parsed.tzinfo is not None else None


def _without_null_characters(value: Any) -> Any:
    """Replace the NUL character in every string: PostgreSQL's JSONB cannot hold it."""
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, list):
        return [_without_null_characters(item) for item in value]
    if isinstance(value, dict):
        return {
            _without_null_characters(key): _without_null_characters(item)
            for key, item in value.items()
        }
    return value
