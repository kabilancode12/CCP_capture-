"""A readable conversation: what the developer asked, what Claude answered, what it did.

The text is made from the chat's main-transcript events (not from the raw lines): prompts and
replies in full, thinking notes shortened, tool calls as one line each (``Edit src/a.py``), and a
short note for each tool result. Sub-agent conversations are not included; they are in
``transcript/agents/``. Events on a rewound branch are shown and marked ``(rewound)``, because
they did happen.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ccp.catalog.loader import ChatData, Row

THINKING_MAX = 600
ERROR_MAX = 100
INPUT_MAX = 120
SHORT_MAX = 200

# The tool input fields that say best what a call is about, most telling first.
_TOOL_FIELDS = ("file_path", "path", "command", "pattern", "url", "query", "description", "prompt")
_TAGS = re.compile(r"</?[a-z-]+>")


def squash(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters: whitespace collapsed, cut with ``...``."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def text_of(content: Any) -> str:
    """The text of a message: a plain string, or its text blocks joined by blank lines."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    return "\n\n".join(parts)


def summarise_tool_call(name: str, tool_input: Any) -> str:
    """One line for a tool call: the tool and the most telling input (a file, a command ...)."""
    detail = ""
    if isinstance(tool_input, dict):
        for key in _TOOL_FIELDS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                detail = squash(value, INPUT_MAX).replace("`", "'")
                break
    return f"**{name}** `{detail}`" if detail else f"**{name}**"


def _clock(moment: datetime | None) -> str:
    return moment.astimezone(UTC).strftime("%H:%M:%S") if moment else "--:--:--"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


def _result_lines(content: Any) -> list[str]:
    """A short note for each tool result of a message: ``ok``, or the first line of an error."""
    lines = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("is_error") is True:
            message = squash(text_of(block.get("content")), ERROR_MAX)
            lines.append(f"  - result: error{': ' + message if message else ''}")
        else:
            lines.append("  - result: ok")
    return lines


def _assistant_lines(content: Any, started: bool, clock: str) -> list[str]:
    """Reply text, thinking notes and tool-call lines of one assistant message."""
    out: list[str] = []
    label = [] if started else [f"**Claude** ({clock})", ""]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            out += [*label, block["text"].strip(), ""]
            label = []
        elif kind == "thinking" and isinstance(block.get("thinking"), str) and block["thinking"]:
            out += [*label, f"> *Thinking:* {squash(block['thinking'], THINKING_MAX)}", ""]
            label = []
        elif kind == "redacted_thinking":
            out += [*label, "> *Thinking: not recorded*", ""]
            label = []
        elif kind == "tool_use":
            out += [
                *label,
                f"- {summarise_tool_call(str(block.get('name', '?')), block.get('input'))}",
            ]
            label = []
    return out


def _compaction_line(compact: Any) -> str:
    if not isinstance(compact, dict):
        return "*The conversation was compacted.*"
    trigger = compact.get("trigger", "?")
    before, after = compact.get("preTokens"), compact.get("postTokens")
    sizes = f", {before} to {after} tokens" if before is not None and after is not None else ""
    return f"*The conversation was compacted ({trigger}{sizes}).*"


def _flag(event: Row) -> str:
    return " (rewound)" if event["is_abandoned"] else ""


def render_conversation(data: ChatData) -> str:
    """The readable conversation of a chat, as Markdown."""
    chat = data.chat
    slash = {row["event_id"]: row for row in data.slash_commands}
    out = [f"# {chat['title'] or 'Untitled chat'}", "", f"Chat `{chat['chat_id']}`", ""]
    prompt_number = 0
    in_reply = False  # a run of assistant messages gets one "Claude" label
    for event in data.events:
        kind, clock = event["event_type"], _clock(event["occurred_at"])
        if kind in ("response", "tool_call", "thinking"):
            lines = _assistant_lines(event["content"], in_reply, clock)
            if lines and event["is_abandoned"] and not in_reply:
                lines[0] += " (rewound)"
            if lines and out and out[-1] != "" and not lines[0].startswith("- "):
                out.append("")  # a paragraph must not run on from the list of tool calls above
            out += lines
            in_reply = in_reply or bool(lines)
            continue
        if kind == "tool_result":
            out += _result_lines(event["content"])
            continue
        if out and out[-1] != "":
            out.append("")
        in_reply = False
        if kind == "prompt" and event["actor"] == "human":
            prompt_number += 1
            out += [f"## Prompt {prompt_number} ({clock}){_flag(event)}", ""]
            out += [_quote(text_of(event["content"])), ""]
        elif kind == "slash_command":
            row = slash.get(event["event_id"], {})
            command = f"{row.get('name') or '?'} {row.get('arguments') or ''}".strip()
            out += [f"**Command** `{command}` ({clock}){_flag(event)}", ""]
        elif kind == "compact_boundary":
            out += [_compaction_line(event["compact"]), ""]
        elif kind == "agent_message":
            text = squash(_TAGS.sub(" ", text_of(event["content"])), SHORT_MAX)
            out += [f"*Message from a sub-agent or background task:* {text}", ""]
        elif kind == "command_output":
            text = squash(_TAGS.sub(" ", text_of(event["content"])), SHORT_MAX)
            out += [f"*Command output:* {text}", ""]
    return "\n".join(out).rstrip() + "\n"
