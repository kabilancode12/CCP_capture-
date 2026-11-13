"""Which edit wrote a line? Replaying recorded edits.

Claude Code's Edit and Write results carry the changed line ranges as a unified-diff style patch
(fact 11 in docs/verify/transcript-facts.md). Replaying a file's edits **in order** tells, for each
line of the file as it is after the last edit, which edit wrote it: a line that an edit adds is
owned by that edit, a line that an edit leaves alone keeps its earlier owner, and later edits shift
lines up and down.

What the replay cannot know: lines written before capture started, changes made by a person or by a
shell command, and edits that were rewound and not recorded. Those lines have **no owner** (None).
If such a change moved lines, the answer for the lines below it is off by the amount moved; the
caller says so.

The module is pure: edits in, owners out.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Hunk:
    """One patch hunk, in the coordinates of the file *before* the edit."""

    old_start: int  # 1-based; for a hunk with no old lines, the line *before* the insertion
    old_lines: int
    lines: tuple[str, ...]  # each starts with " " (kept), "-" (removed) or "+" (added)


@dataclass(frozen=True)
class Edit:
    """One recorded edit of a file."""

    change_id: int
    hunks: tuple[Hunk, ...] = ()
    created_lines: int | None = None  # set when the edit wrote the whole file (a new file)


def replay(edits: Iterable[Edit]) -> list[int | None]:
    """Replay edits in order and return the owner of each line.

    Args:
        edits: The file's edits, oldest first.

    Returns:
        A list where item ``i`` is the ``change_id`` of the edit that wrote line ``i + 1``, or None
        if no recorded edit did. Its length is the longest extent the edits reveal; lines beyond it
        are unknown.
    """
    owners: list[int | None] = []
    for edit in edits:
        if edit.created_lines is not None:
            owners = [edit.change_id] * edit.created_lines
            continue
        # Hunks refer to the file before the edit, so apply the last one first: the earlier ones
        # then still sit where the patch says they do.
        for hunk in sorted(edit.hunks, key=lambda h: h.old_start, reverse=True):
            _apply(owners, hunk, edit.change_id)
    return owners


def _apply(owners: list[int | None], hunk: Hunk, change_id: int) -> None:
    """Apply one hunk to the list of owners, in place."""
    start = hunk.old_start - 1 if hunk.old_lines > 0 else hunk.old_start
    start = max(start, 0)
    if len(owners) < start + hunk.old_lines:
        owners.extend([None] * (start + hunk.old_lines - len(owners)))

    replacement: list[int | None] = []
    position = start  # where we are in the old file
    for line in hunk.lines:
        marker = line[:1]
        if marker == " ":
            if position >= len(owners):  # a patch that runs past what we know of the file
                owners.extend([None] * (position + 1 - len(owners)))
            replacement.append(owners[position])
            position += 1
        elif marker == "-":
            position += 1
        elif marker == "+":
            replacement.append(change_id)
        # Anything else (such as "\ No newline at end of file") is not a line of the file.
    owners[start:position] = replacement


def owner_of(owners: Sequence[int | None], line: int) -> int | None:
    """The owner of a 1-based line number, or None if unknown or out of range."""
    return owners[line - 1] if 1 <= line <= len(owners) else None
