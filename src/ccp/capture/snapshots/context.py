"""What a snapshot should say about itself: who made the change, with which model, why.

Hooks know the tool and its call id. The model and the developer's prompt are read from the end of
the session transcript (the last assistant message and the last real prompt). The prompt excerpt is
redacted, because it is written into a commit message that leaves the machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ccp.redaction.redactor import redact_string

TAIL_BYTES = 262_144  # how much of the transcript's end is read
EXCERPT_CHARS = 160
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

HUMAN = "human"
AI_EDIT = "ai_edit"
AI_COMMAND = "ai_command"
AI_OTHER = "ai_other"
SUBAGENT = "subagent"


@dataclass(frozen=True)
class SnapshotContext:
    """The cause of a snapshot (all optional except the actor).

    Attributes:
        actor: ``human``, ``ai_edit`` (Edit/Write tools), ``ai_command`` (a shell command),
            ``ai_other`` (any other tool, or changes seen at the end of a reply) or ``subagent``.
        model: The model of the assistant message that acted.
        tool: The tool name.
        target: The file or command the tool worked on, shortened.
        tool_use_id: The tool call id (links the snapshot to the transcript).
        prompt_id: The uuid of the developer prompt behind the change.
        prompt_excerpt: The start of that prompt, redacted.
        agent: ``main`` or the sub-agent's type.
    """

    actor: str
    model: str | None = None
    tool: str | None = None
    target: str | None = None
    tool_use_id: str | None = None
    prompt_id: str | None = None
    prompt_excerpt: str | None = None
    agent: str = "main"


def actor_of_tool(tool_name: str | None, agent: str | None = None) -> str:
    """The actor class of a tool call."""
    if agent:
        return SUBAGENT
    if tool_name in _EDIT_TOOLS:
        return AI_EDIT
    if tool_name in _SHELL_TOOLS:
        return AI_COMMAND
    return AI_OTHER


def one_line(text: str, limit: int) -> str:
    """Redacted, control-free, one line of at most ``limit`` characters."""
    cleaned, _ = redact_string(_CONTROL.sub(" ", text).strip())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."


def target_of(tool_input: Any) -> str | None:
    """The file path or the start of the command a tool call worked on."""
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return one_line(value, 80)
    return None


def read_transcript_context(transcript: Path | None) -> tuple[str | None, str | None, str | None]:
    """``(model, prompt uuid, prompt excerpt)`` from the end of a transcript; None where unknown."""
    if transcript is None:
        return None, None, None
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None, None, None
    model = prompt_id = excerpt = None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        raw_message = entry.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        if model is None and entry.get("type") == "assistant":
            found = message.get("model")
            if isinstance(found, str) and found != "<synthetic>":
                model = found
        if prompt_id is None and entry.get("type") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if isinstance(content, str) and content.strip() and not content.startswith("<"):
                prompt_id = str(entry.get("uuid") or "") or None
                excerpt = one_line(content, EXCERPT_CHARS)
        if model and prompt_id:
            break
    return model, prompt_id, excerpt
