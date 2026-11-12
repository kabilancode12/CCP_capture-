"""Inventory of Claude Code session transcripts (JSON lines files).

The indexer in later phases depends on facts about transcripts that are not documented
(for example whether thinking text is written, or how a rewind appears). This module reads
real transcripts and reports those facts as Markdown, so the design is based on evidence.

Nothing here modifies a transcript, and no raw content is printed: only entry types, key
names, counts and masked shapes appear in the report (see :mod:`ccp.verify.masking`).

Every rule below that guesses at Claude Code's format is marked ``[verify]``.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ccp.verify.masking import shape_of

JsonObject = dict[str, Any]

# [verify] Tools whose results may carry a structured patch (line ranges) in `toolUseResult`.
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


@dataclass
class TranscriptFacts:
    """Everything learned from one or more transcripts.

    Attributes:
        files: Transcript files that were read.
        line_count: Non-empty lines read.
        invalid_lines: Lines that were not valid JSON objects.
        entry_types: Number of entries per top-level ``type`` value.
        keys_by_type: Set of top-level keys seen for each entry type.
        assistant_messages: Number of assistant entries.
        with_model: Assistant entries that name a model.
        with_usage: Assistant entries that report token usage.
        block_types: Number of content blocks per block type (text, tool_use, thinking, ...).
        thinking_with_text: Thinking blocks that contain readable text.
        thinking_signature_only: Thinking blocks with a signature but no text.
        edit_result_shapes: Masked shape of ``toolUseResult`` for each edit-type tool.
        sidechain_entries: Entries flagged ``isSidechain`` (sub-agent activity).
        compaction_markers: Entries that look like compaction records, by label.
        session_ids: Distinct session identifiers seen.
        entries_with_uuid: Entries carrying a ``uuid``.
        entries_with_parent: Entries carrying a ``parentUuid``.
        prompt_children_per_parent: How many user *prompts* point at each ``parentUuid``.
            Only prompts count: a tool call's result and the next assistant entry legitimately
            share a parent, so counting all children would report false rewinds.
        bytes_by_type: Size in bytes of the lines of each entry type.
        denied_tool_calls: Tool results marked with ``toolDenialKind`` (permission denied).
        subagent_files: Transcript files found in a ``subagents`` folder.
    """

    files: list[str] = field(default_factory=list)
    line_count: int = 0
    invalid_lines: int = 0
    entry_types: Counter[str] = field(default_factory=Counter)
    keys_by_type: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    assistant_messages: int = 0
    with_model: int = 0
    with_usage: int = 0
    block_types: Counter[str] = field(default_factory=Counter)
    thinking_with_text: int = 0
    thinking_signature_only: int = 0
    edit_result_shapes: dict[str, Any] = field(default_factory=dict)
    sidechain_entries: int = 0
    compaction_markers: Counter[str] = field(default_factory=Counter)
    session_ids: set[str] = field(default_factory=set)
    entries_with_uuid: int = 0
    entries_with_parent: int = 0
    prompt_children_per_parent: Counter[str] = field(default_factory=Counter)
    bytes_by_type: Counter[str] = field(default_factory=Counter)
    denied_tool_calls: int = 0
    subagent_files: int = 0

    @property
    def branch_points(self) -> int:
        """Number of entries followed by two or more user prompts (a rewind).

        Observed on Claude Code 2.1.289: after a rewind, the old prompt stays in the file and
        the new prompt becomes a second child of the same entry.
        """
        return sum(1 for count in self.prompt_children_per_parent.values() if count > 1)


def find_transcripts(paths: list[Path]) -> list[Path]:
    """Expand the given paths into a sorted list of ``.jsonl`` files.

    Args:
        paths: Files, or folders to search recursively (for example a session folder that
            also holds sub-agent transcripts).

    Returns:
        Every transcript file found, in a stable order.
    """
    found: list[Path] = []
    for path in paths:
        found.extend(sorted(path.rglob("*.jsonl")) if path.is_dir() else [path])
    return found


def inspect_files(paths: list[Path]) -> TranscriptFacts:
    """Read every transcript and collect the facts.

    Args:
        paths: Transcript files or folders (see :func:`find_transcripts`).

    Returns:
        The combined facts for all files.
    """
    facts = TranscriptFacts()
    for file_path in find_transcripts(paths):
        facts.files.append(str(file_path))
        # Observed layout: <session-id>/subagents/agent-<agent-id>.jsonl
        facts.subagent_files += file_path.parent.name == "subagents"
        # Maps a tool call id to its tool name, so a later result can be matched to its tool.
        tool_names: dict[str, str] = {}
        with file_path.open(encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                if raw_line.strip():
                    _record_line(facts, raw_line, tool_names)
    return facts


def _record_line(facts: TranscriptFacts, raw_line: str, tool_names: dict[str, str]) -> None:
    """Parse one line and update the facts. Invalid lines are counted, not fatal."""
    facts.line_count += 1
    try:
        entry = json.loads(raw_line)
    except json.JSONDecodeError:
        facts.invalid_lines += 1
        return
    if not isinstance(entry, dict):
        facts.invalid_lines += 1
        return

    entry_type = str(entry.get("type", "<missing>"))
    facts.entry_types[entry_type] += 1
    facts.bytes_by_type[entry_type] += len(raw_line.strip().encode("utf-8"))
    facts.keys_by_type[entry_type].update(entry.keys())
    facts.denied_tool_calls += "toolDenialKind" in entry
    _record_identity_and_tree(facts, entry)
    _record_compaction(facts, entry, entry_type)

    message = entry.get("message")
    if isinstance(message, dict):
        _record_message(facts, entry, message, tool_names)


def _record_identity_and_tree(facts: TranscriptFacts, entry: JsonObject) -> None:
    """Record session ids, sub-agent flag and the uuid/parentUuid links of the entry."""
    if entry.get("sessionId"):
        facts.session_ids.add(str(entry["sessionId"]))
    if entry.get("isSidechain"):
        facts.sidechain_entries += 1
    if entry.get("uuid"):
        facts.entries_with_uuid += 1
    if entry.get("parentUuid"):
        facts.entries_with_parent += 1
        if _is_user_prompt(entry):
            facts.prompt_children_per_parent[str(entry["parentUuid"])] += 1


def _is_user_prompt(entry: JsonObject) -> bool:
    """Return True for a user entry that is a typed prompt, not a tool result."""
    if entry.get("type") != "user" or "toolUseResult" in entry:
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    has_tool_result = isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )
    return not has_tool_result


def _record_compaction(facts: TranscriptFacts, entry: JsonObject, entry_type: str) -> None:
    """Note entries that look like compaction records.

    [verify] Compaction may appear as a ``summary`` entry, a ``compact_boundary`` system
    entry, or a user entry flagged ``isCompactSummary``. All three are checked.
    """
    if entry_type == "summary":
        facts.compaction_markers["type=summary"] += 1
    if entry.get("subtype") == "compact_boundary":
        facts.compaction_markers["subtype=compact_boundary"] += 1
    if entry.get("isCompactSummary"):
        facts.compaction_markers["isCompactSummary"] += 1


def _record_message(
    facts: TranscriptFacts, entry: JsonObject, message: JsonObject, tool_names: dict[str, str]
) -> None:
    """Record model/usage presence and every content block of a message."""
    if message.get("role") == "assistant":
        facts.assistant_messages += 1
        facts.with_model += bool(message.get("model"))
        facts.with_usage += bool(message.get("usage"))

    content = message.get("content")
    if not isinstance(content, list):
        return  # plain-string content has no blocks to inspect
    for block in content:
        if isinstance(block, dict):
            _record_block(facts, entry, block, tool_names)


def _record_block(
    facts: TranscriptFacts, entry: JsonObject, block: JsonObject, tool_names: dict[str, str]
) -> None:
    """Record one content block: thinking details, tool calls and edit results."""
    block_type = str(block.get("type", "<missing>"))
    facts.block_types[block_type] += 1

    if block_type == "thinking":
        # [verify] A thinking block may hold readable text, or only a signature.
        if block.get("thinking"):
            facts.thinking_with_text += 1
        else:
            facts.thinking_signature_only += 1
    elif block_type == "tool_use" and block.get("id"):
        tool_names[str(block["id"])] = str(block.get("name", ""))
    elif block_type == "tool_result":
        tool_name = tool_names.get(str(block.get("tool_use_id", "")), "")
        # [verify] The structured patch is expected in the entry's `toolUseResult` field.
        # It holds file content, so every string is hidden (max_visible=0): keys and types
        # only. A short secret in an edited file must never appear in the report.
        if tool_name in EDIT_TOOLS and "toolUseResult" in entry:
            shape = shape_of(entry["toolUseResult"], max_visible=0)
            facts.edit_result_shapes.setdefault(tool_name, shape)


# ---------------------------------------------------------------- report


def render_markdown(facts: TranscriptFacts) -> str:
    """Turn the facts into a Markdown report that can be pasted into the facts note.

    Args:
        facts: The result of :func:`inspect_files`.

    Returns:
        The report text.
    """
    lines = ["# Transcript inspection", ""]
    lines += [f"- Files read: {len(facts.files)}", f"- Lines: {facts.line_count}"]
    lines += [f"- Invalid lines: {facts.invalid_lines}", f"- Session ids: {len(facts.session_ids)}"]

    lines += ["", "## Entry types and keys seen", ""]
    lines += ["| type | count | bytes | keys |", "| --- | --- | --- | --- |"]
    for entry_type, count in facts.entry_types.most_common():
        keys = ", ".join(sorted(facts.keys_by_type[entry_type]))
        lines.append(f"| {entry_type} | {count} | {facts.bytes_by_type[entry_type]} | {keys} |")

    lines += ["", "## Assistant messages", ""]
    lines += [f"- Total: {facts.assistant_messages}"]
    lines += [f"- With model: {facts.with_model}", f"- With token usage: {facts.with_usage}"]

    lines += ["", "## Content blocks", ""]
    lines += [f"- {name}: {count}" for name, count in facts.block_types.most_common()]
    lines += ["", "## Thinking [verify]", ""]
    lines += [f"- With readable text: {facts.thinking_with_text}"]
    lines += [f"- Signature only: {facts.thinking_signature_only}"]
    lines += [f"- Redacted blocks: {facts.block_types.get('redacted_thinking', 0)}"]

    lines += ["", "## Structured edit results [verify]", ""]
    if facts.edit_result_shapes:
        for tool, shape in facts.edit_result_shapes.items():
            lines += [f"- {tool}: `{json.dumps(shape)}`"]
    else:
        lines += ["- None found (no `toolUseResult` on edit-tool results)."]

    lines += ["", "## Conversation tree [verify]", ""]
    lines += [f"- Entries with uuid: {facts.entries_with_uuid}"]
    lines += [f"- Entries with parentUuid: {facts.entries_with_parent}"]
    lines += [f"- Rewind points (an entry followed by 2+ user prompts): {facts.branch_points}"]
    lines += [f"- Denied tool calls (toolDenialKind): {facts.denied_tool_calls}"]

    lines += ["", "## Sub-agents and compaction [verify]", ""]
    lines += [f"- Sidechain entries: {facts.sidechain_entries}"]
    lines += [f"- Sub-agent transcript files: {facts.subagent_files}"]
    markers = dict(facts.compaction_markers) or "none"
    lines += [f"- Compaction markers: {markers}"]
    return "\n".join(lines) + "\n"
