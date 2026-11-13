"""Finding file changes in a transcript entry.

When Claude Code edits or writes a file, the result entry carries ``toolUseResult`` with the file's
path and a ``structuredPatch``: the changed line ranges, for free (fact 11 in
docs/verify/transcript-facts.md). So the indexer needs no snapshots to know **which lines an AI edit
touched**. Observed on 2.1.289:

* Edit: ``filePath``, ``structuredPatch`` (hunks with ``oldStart``, ``oldLines``, ``newStart``,
  ``newLines``, ``lines``), ``userModified``.
* Write, new file: ``type: "create"``, ``filePath``, ``content`` and an **empty** patch.
* Write, existing file: ``type: "update"`` and a patch.

The rule is by shape, not by tool name, so another tool that reports its change the same way is
found too. A result of any other shape (a failed tool gives a plain string) is not a file change.
What this cannot see: files changed by a shell command, and changes made by a person.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ccp.index.parser import content_blocks

ADDED = "added"
MODIFIED = "modified"


@dataclass(frozen=True)
class FileChange:
    """One AI edit of one file."""

    tool_use_id: str | None  # the id of the call that made it
    path: str  # relative to the session's folder when inside it, else absolute; "/" separators
    absolute_path: str  # as Claude Code reported it
    change_type: str  # added | modified
    user_modified: bool | None  # the developer changed the proposed edit before accepting it
    lines_added: int
    lines_removed: int
    hunks: tuple[dict[str, int], ...]  # {old_start, old_lines, new_start, new_lines}


def extract_file_change(entry: dict[str, Any]) -> FileChange | None:
    """Read the file change in a ``user`` entry that holds a tool result, if there is one.

    Args:
        entry: The transcript entry (the payload of an event).

    Returns:
        The change, or None if the entry is not the result of an edit.
    """
    result = entry.get("toolUseResult")
    if not isinstance(result, dict):
        return None
    path = result.get("filePath")
    patch = result.get("structuredPatch")
    if not isinstance(path, str) or not path or not isinstance(patch, list):
        return None

    hunks = tuple(h for h in (_hunk(item) for item in patch) if h is not None)
    added = removed = 0
    for item in patch:
        lines = item.get("lines") if isinstance(item, dict) else None
        for line in lines if isinstance(lines, list) else ():
            if isinstance(line, str) and line.startswith("+"):
                added += 1
            elif isinstance(line, str) and line.startswith("-"):
                removed += 1

    created = result.get("type") == "create"
    if created and not hunks:
        # A new file has no patch: the whole content is the change.
        content = result.get("content")
        count = len(content.splitlines()) if isinstance(content, str) else 0
        if count:
            hunks = ({"old_start": 0, "old_lines": 0, "new_start": 1, "new_lines": count},)
        added = count

    user_modified = result.get("userModified")
    cwd = entry.get("cwd")
    return FileChange(
        tool_use_id=_tool_use_id(content_blocks(entry)),
        path=_relative_path(path, cwd if isinstance(cwd, str) else None),
        absolute_path=path,
        change_type=ADDED if created else MODIFIED,
        user_modified=user_modified if isinstance(user_modified, bool) else None,
        lines_added=added,
        lines_removed=removed,
        hunks=hunks,
    )


def _hunk(item: object) -> dict[str, int] | None:
    """One patch hunk as four whole numbers, or None if it does not have them."""
    if not isinstance(item, dict):
        return None
    numbers = {}
    for ours, theirs in (
        ("old_start", "oldStart"),
        ("old_lines", "oldLines"),
        ("new_start", "newStart"),
        ("new_lines", "newLines"),
    ):
        value = item.get(theirs)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        numbers[ours] = value
    return numbers


def _tool_use_id(blocks: list[dict[str, Any]]) -> str | None:
    """The id of the call that the first tool result answers."""
    for block in blocks:
        value = block.get("tool_use_id")
        if block.get("type") == "tool_result" and isinstance(value, str) and value:
            return value
    return None


def _relative_path(path: str, cwd: str | None) -> str:
    """The path with ``/`` separators, relative to ``cwd`` if the file is inside it.

    A file inside the session's folder gets the same path on every machine, which is what a file
    history needs. Windows paths are compared without regard to case.
    """
    clean = path.replace("\\", "/")
    if not cwd:
        return clean
    folder = cwd.replace("\\", "/").rstrip("/")
    windows = len(clean) > 1 and clean[1] == ":"
    inside = (clean.lower() if windows else clean).startswith(
        (folder.lower() if windows else folder) + "/"
    )
    return clean[len(folder) + 1 :] if inside else clean
