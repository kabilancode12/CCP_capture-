"""Which session produced a commit? Matching the lines a commit adds to recorded AI edits.

A developer's commit says nothing about Claude Code. But the lines it adds are, for AI-written code,
the same lines that a session's Edit and Write results recorded as added. So for each session the
share of the commit's added lines that the session's edits also added tells how much of the commit
that session wrote: its **confidence**.

Rules, kept simple on purpose:

* Lines are compared by their text, stripped of surrounding space, **within the same file**.
* Lines shorter than ``MIN_LINE_LENGTH`` characters (``}``, ``end``, a blank line) are ignored:
  they appear everywhere and would link every commit to every session.
* A session's line counts once per time the session added it, so a line the commit adds twice is
  explained twice only if the session added it twice.
* Only edits made at or before the commit count.

The module is pure: parsed diffs in, links out. Reading Git and the database is elsewhere.

What it cannot do: tell AI-written code that a person then retyped from code they wrote themselves
that happens to be identical, find code that a formatter changed after the AI wrote it, or link a
commit made before capture started.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

MIN_LINE_LENGTH = 4
DEFAULT_MIN_CONFIDENCE = 0.3
# An edit recorded this long after the commit's time is still taken to be before it: clocks differ
# between the developer's machine and the commit.
CLOCK_SLACK_SECONDS = 300


@dataclass(frozen=True)
class AddedLines:
    """A line of text that an AI edit added to a file, and when."""

    session_id: str
    path: str  # as the edit recorded it (relative to the session folder)
    text: str
    occurred_at: datetime | None


@dataclass(frozen=True)
class Link:
    """A commit's link to a session."""

    session_id: str
    confidence: float
    matched_lines: int
    total_lines: int


def significant(text: str) -> str | None:
    """The line stripped of surrounding space, or None if it is too short to tell anything."""
    stripped = text.strip()
    return stripped if len(stripped) >= MIN_LINE_LENGTH else None


def same_file(commit_path: str, edit_path: str) -> bool:
    """Whether a path in a commit and a path in an edit are the same file.

    The commit's path is relative to the repository root; the edit's is relative to the folder the
    session ran in, which can be a sub-folder. So one may be the tail of the other, on a folder
    boundary (``app/src/a.py`` and ``src/a.py`` are the same; ``xsrc/a.py`` and ``src/a.py`` are
    not).
    """
    a, b = commit_path.strip("/"), edit_path.strip("/")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def link_commit(
    commit_added: Mapping[str, list[str]],
    committed_at: datetime | None,
    ai_lines: Iterable[AddedLines],
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[Link]:
    """Link one commit to the sessions that wrote its lines.

    Args:
        commit_added: The lines the commit adds, per file path.
        committed_at: When the commit was made (None: every edit counts).
        ai_lines: The lines that recorded AI edits added (any session, any file).
        min_confidence: Sessions that explain a smaller share of the commit are not linked.

    Returns:
        The links, most confident first. Empty if the commit adds nothing to compare.
    """
    wanted: dict[str, Counter[str]] = {}
    for path, lines in commit_added.items():
        counted = Counter(s for s in map(significant, lines) if s is not None)
        if counted:
            wanted[path] = counted
    total = sum(sum(c.values()) for c in wanted.values())
    if total == 0:
        return []

    # What each session added, per commit file. A session's lines are matched against the file
    # they were added to, so the same text in another file does not count.
    offered: dict[str, dict[str, Counter[str]]] = {}
    for item in ai_lines:
        if committed_at and item.occurred_at:
            late = (item.occurred_at - committed_at).total_seconds() > CLOCK_SLACK_SECONDS
        else:
            late = False
        if late:
            continue
        text = significant(item.text)
        if text is None:
            continue
        for path in wanted:
            if same_file(path, item.path):
                offered.setdefault(item.session_id, {}).setdefault(path, Counter())[text] += 1

    links = []
    for session_id, per_path in offered.items():
        matched = sum(
            sum(min(count, per_path[path][text]) for text, count in wanted[path].items())
            for path in per_path
        )
        confidence = matched / total
        if matched and confidence >= min_confidence:
            links.append(Link(session_id, round(confidence, 3), matched, total))
    return sorted(links, key=lambda link: (-link.confidence, link.session_id))
