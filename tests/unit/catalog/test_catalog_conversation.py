from datetime import UTC, datetime
from typing import Any

from ccp.catalog.conversation import render_conversation, summarise_tool_call
from ccp.catalog.loader import ChatData


def _event(kind: str, content: Any, minute: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "event_id": extra.pop("event_id", minute),
        "event_type": kind,
        "actor": extra.pop("actor", "ai"),
        "occurred_at": datetime(2026, 7, 1, 10, minute, tzinfo=UTC),
        "is_abandoned": extra.pop("is_abandoned", False),
        "raw_line_number": minute + 1,
        "content": content,
        "compact": extra.pop("compact", None),
    }


def _tool(name: str, **tool_input: Any) -> list[dict[str, Any]]:
    return [{"type": "tool_use", "id": "t", "name": name, "input": tool_input}]


def _data(events: list[dict[str, Any]], slash: list[dict[str, Any]] | None = None) -> ChatData:
    chat = {"chat_id": "c1", "title": "Fix the bug"}
    return ChatData(chat=chat, events=events, slash_commands=slash or [])


def test_tool_call_summary() -> None:
    assert summarise_tool_call("Edit", {"file_path": "src/a.py", "old_string": "x"}) == (
        "**Edit** `src/a.py`"
    )
    assert summarise_tool_call("Bash", {"command": "pytest  -q\n--x `y`"}) == (
        "**Bash** `pytest -q --x 'y'`"
    )
    assert summarise_tool_call("Mystery", {"n": 1}) == "**Mystery**"
    long = summarise_tool_call("Bash", {"command": "x" * 500})
    assert len(long) < 150 and long.endswith("...`")


def test_prompts_replies_tools_and_results_are_readable() -> None:
    events = [
        _event("prompt", "Fix the bug\nplease", 0, actor="human"),
        _event("thinking", [{"type": "thinking", "thinking": "Let me  look."}], 1),
        _event(
            "tool_call", [{"type": "text", "text": "Looking."}, *_tool("Edit", file_path="a.py")], 2
        ),
        _event(
            "tool_result",
            [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}],
            3,
            actor="system",
        ),
        _event("tool_call", _tool("Bash", command="pytest"), 4),
        _event(
            "tool_result",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t",
                    "is_error": True,
                    "content": "Exit code 1\n2 failed",
                }
            ],
            5,
            actor="system",
        ),
        _event("response", [{"type": "text", "text": "Done."}], 6),
    ]
    text = render_conversation(_data(events))
    assert text.startswith("# Fix the bug\n")
    assert "## Prompt 1 (10:00:00)\n\n> Fix the bug\n> please" in text
    assert "> *Thinking:* Let me look." in text
    assert text.count("**Claude**") == 1  # one label for a run of assistant messages
    assert "- **Edit** `a.py`\n  - result: ok" in text
    assert "- **Bash** `pytest`\n  - result: error: Exit code 1 2 failed" in text
    assert "\n\nDone.\n" in text  # a paragraph after the list is not part of the list


def test_rewound_prompt_slash_command_and_compaction_are_marked() -> None:
    events = [
        _event("prompt", "old", 0, actor="human", is_abandoned=True),
        _event(
            "compact_boundary",
            None,
            1,
            compact={"trigger": "auto", "preTokens": 9, "postTokens": 2},
        ),
        _event(
            "slash_command", "<command-name>/model</command-name>", 2, actor="human", event_id=77
        ),
        _event("prompt", "new", 3, actor="human"),
    ]
    slash = [{"event_id": 77, "name": "/model", "arguments": "opus"}]
    text = render_conversation(_data(events, slash))
    assert "## Prompt 1 (10:00:00) (rewound)" in text
    assert "## Prompt 2 (10:03:00)\n" in text
    assert "*The conversation was compacted (auto, 9 to 2 tokens).*" in text
    assert "**Command** `/model opus` (10:02:00)" in text


def test_redacted_thinking_and_empty_chat_do_not_break() -> None:
    events = [_event("thinking", [{"type": "redacted_thinking"}], 0)]
    assert "*Thinking: not recorded*" in render_conversation(_data(events))
    assert render_conversation(_data([])).startswith("# Fix the bug")
    untitled = ChatData(chat={"chat_id": "c", "title": None})
    assert "# Untitled chat" in render_conversation(untitled)
