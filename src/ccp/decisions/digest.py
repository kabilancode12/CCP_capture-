"""A compact text of one chat, built from the index, for the model to read.

The digest lists what happened in the order it happened, one line each, and gives every line a
number: ``[L12] PROMPT ...``. The model cites these numbers as evidence. :class:`Digest.refs` maps
each number back to the raw transcript line (object key and line number) and to the event, so that a
decision stores a durable pointer into layer 1 rather than an event id that a rebuild would change.

What is listed: the developer's prompts and Claude's replies (short excerpts), slash commands,
file edits (one line per file), test runs with their pass / fail counts, corrections found by the
indexer, and the commits linked to the chat. The transcript is already redacted on the developer
machine; the digest only reads it.

The digest is cut to a character limit: excerpts get shorter first, then replies are dropped, then
the middle of the chat is left out (the start and the end are kept).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pg8000.dbapi

from ccp.decisions.shellfiles import files_changed_by

# How long a prompt or reply excerpt may be, tried in this order until the digest fits.
EXCERPT_WIDTHS = (800, 400, 200, 100)

# How much text of an event is read from the database before the excerpt is cut again.
_READ_CHARS = 1200

_MAIN_STREAM = "main"
_EARLIEST = datetime.min.replace(tzinfo=UTC)

# The text of a prompt or reply: a string, or the first text block of a block list.
_TEXT = """
    CASE WHEN jsonb_typeof(e.payload #> '{message,content}') = 'string'
         THEN e.payload #>> '{message,content}'
         ELSE jsonb_path_query_first(
                  e.payload, '$.message.content[*] ? (@.type == "text").text') #>> '{}'
    END
"""

_SESSION = "SELECT title, started_at, ended_at, branch FROM session WHERE session_id = %s"

_MESSAGES = f"""
    SELECT e.event_id, e.event_type, e.occurred_at, e.raw_object_key, e.raw_line_number,
           left({_TEXT}, {_READ_CHARS})
      FROM event e
     WHERE e.session_id = %s AND NOT e.is_abandoned
       AND split_part(e.raw_object_key, '/', 3) = '{_MAIN_STREAM}'
       AND ((e.event_type = 'prompt' AND e.actor = 'human')
            OR (e.event_type = 'response' AND e.actor = 'ai'))
"""  # noqa: S608  (only constants are inserted)

_COMMANDS = f"""
    SELECT c.event_id, c.occurred_at, e.raw_object_key, e.raw_line_number,
           left(c.name, 80), left(c.arguments, 200)
      FROM v_slash_command c JOIN event e ON e.event_id = c.event_id
     WHERE c.session_id = %s AND NOT c.is_abandoned
       AND split_part(e.raw_object_key, '/', 3) = '{_MAIN_STREAM}'
"""  # noqa: S608  (only constants are inserted)

# One line per file and prompt: the first edit is the reference; counts cover the edits that one
# prompt of the developer led to, so a file that changed for two decisions is on two lines.
_EDITS = """
    SELECT (array_agg(e.event_id ORDER BY e.occurred_at, e.event_id))[1],
           min(e.occurred_at),
           (array_agg(e.raw_object_key ORDER BY e.occurred_at, e.event_id))[1],
           (array_agg(e.raw_line_number ORDER BY e.occurred_at, e.event_id))[1],
           f.path, count(*), sum(f.lines_added), sum(f.lines_removed), max(f.file_role),
           bool_or(f.change_type = 'added'), bool_and(f.change_type = 'deleted')
      FROM file_change f JOIN event e ON e.event_id = f.event_id
     WHERE f.session_id = %s AND NOT e.is_abandoned
     GROUP BY f.path, f.prompt_event_id
"""

# Claude's own thinking notes (the thinking summaries Claude Code saves when showThinkingSummaries
# is on). Most thinking blocks have no text; only those that have are listed.
_THINKING = f"""
    SELECT e.event_id, e.occurred_at, e.raw_object_key, e.raw_line_number, t.text
      FROM thinking t JOIN event e ON e.event_id = t.event_id
     WHERE e.session_id = %s AND NOT e.is_abandoned AND length(btrim(COALESCE(t.text, ''))) > 0
       AND split_part(e.raw_object_key, '/', 3) = '{_MAIN_STREAM}'
"""  # noqa: S608  (only constants are inserted)

# How much of one thinking note is kept (the note is copied from the data, not rewritten).
_NOTE_CHARS = 600

# The Bash commands of each tool call (one call may hold several), for the shell file changes.
_SHELL = """
    SELECT e.event_id, e.occurred_at, e.raw_object_key, e.raw_line_number,
           array(SELECT b -> 'input' ->> 'command'
                   FROM jsonb_array_elements(e.payload #> '{message,content}') b
                  WHERE b ->> 'type' = 'tool_use' AND b ->> 'name' = 'Bash')
      FROM event e
     WHERE e.session_id = %s AND e.event_type = 'tool_call' AND NOT e.is_abandoned
       AND jsonb_typeof(e.payload #> '{message,content}') = 'array'
"""

_TESTS = """
    SELECT e.event_id, e.occurred_at, e.raw_object_key, e.raw_line_number, t.framework, t.passed,
           t.tests_passed, t.tests_failed, t.tests_skipped, left(t.command, 80)
      FROM test_run t JOIN event e ON e.event_id = t.call_event_id
     WHERE t.session_id = %s AND NOT e.is_abandoned
"""

_CORRECTIONS = """
    SELECT e.event_id, e.occurred_at, e.raw_object_key, e.raw_line_number, k.kind, k.corrected_by,
           left(k.note, 150)
      FROM correction k JOIN event e ON e.event_id = k.trigger_event_id
     WHERE k.session_id = %s AND NOT e.is_abandoned
"""

_COMMITS = """
    SELECT commit_sha, committed_at, confidence::float8, matched_lines, total_lines
      FROM commit_link WHERE session_id = %s ORDER BY committed_at NULLS LAST, commit_sha
"""


@dataclass(frozen=True)
class FileRef:
    """A file a digest line changed (read from the recorded data, never written by the model)."""

    path: str
    change: str  # created | modified | deleted
    via: str  # edit (Edit/Write tool) | shell (a command)


@dataclass(frozen=True)
class LineRef:
    """Where one digest line comes from."""

    event_id: int
    raw_object_key: str
    raw_line_number: int
    kind: str  # prompt | reply | command | edit | shell | think | test | correction
    files: tuple[FileRef, ...] = ()  # for edit and shell lines
    note: str = ""  # for think lines: the thinking note as recorded


@dataclass(frozen=True)
class Digest:
    """The text for the model and the map from its line numbers to the raw transcript."""

    text: str
    refs: dict[int, LineRef]

    @property
    def chars(self) -> int:
        """The size of the text."""
        return len(self.text)


@dataclass(frozen=True)
class _Entry:
    """One thing that happened, before it is numbered and written as a line."""

    at: datetime
    ref: LineRef
    label: str  # PROMPT | REPLY | COMMAND | EDIT | SHELL | THINK | TEST | CORRECTION
    excerpt: str = ""  # shortened to the current width
    detail: str = ""  # fixed text that follows the label

    def line(self, number: int, width: int) -> str:
        """Write the line, with the excerpt cut to ``width`` characters."""
        text = _shorten(self.excerpt, width)
        parts = [f"[L{number}] {self.label}", self.detail, text]
        return " ".join(part for part in parts if part)


def build_digest(connection: pg8000.dbapi.Connection, session_id: str, max_chars: int) -> Digest:
    """Build the digest of a chat.

    Args:
        connection: An open connection (only read from).
        session_id: The chat's id.
        max_chars: The largest size of the text.

    Returns:
        The digest. Its text is empty of events when the chat has none (callers check first).
    """
    cursor = connection.cursor()
    header = _header(cursor, session_id)
    entries = sorted(_entries(cursor, session_id), key=_order)
    footer = _footer(cursor, session_id)
    return _fit(header, entries, footer, max_chars)


def _order(entry: _Entry) -> tuple[datetime, str, int]:
    return entry.at, entry.ref.raw_object_key, entry.ref.raw_line_number


def _fit(header: str, entries: list[_Entry], footer: str, max_chars: int) -> Digest:
    """Write the digest, making it smaller step by step until it fits."""
    for width in EXCERPT_WIDTHS:
        digest = _write(header, entries, footer, width)
        if digest.chars <= max_chars:
            return digest
    without_thinking = [entry for entry in entries if entry.ref.kind != "think"]
    digest = _write(header, without_thinking, footer, EXCERPT_WIDTHS[-1])
    if digest.chars <= max_chars:
        return digest
    without_replies = [entry for entry in without_thinking if entry.ref.kind != "reply"]
    digest = _write(header, without_replies, footer, EXCERPT_WIDTHS[-1])
    if digest.chars <= max_chars:
        return digest
    return _cut_middle(header, without_replies, footer, max_chars)


def _write(header: str, entries: list[_Entry], footer: str, width: int) -> Digest:
    refs: dict[int, LineRef] = {}
    lines = [header]
    for number, entry in enumerate(entries, start=1):
        refs[number] = entry.ref
        lines.append(entry.line(number, width))
    lines.append(footer)
    return Digest("\n".join(line for line in lines if line), refs)


def _cut_middle(header: str, entries: list[_Entry], footer: str, max_chars: int) -> Digest:
    """Keep the first and the last lines that fit and say how many were left out."""
    width = EXCERPT_WIDTHS[-1]
    numbered = [(number, entry.line(number, width)) for number, entry in enumerate(entries, 1)]
    budget = max_chars - len(header) - len(footer) - 80
    start: list[tuple[int, str]] = []
    end: list[tuple[int, str]] = []
    used = 0
    low, high = 0, len(numbered) - 1
    take_start = True
    while low <= high:
        number, line = numbered[low] if take_start else numbered[high]
        if used + len(line) + 1 > budget:
            break
        used += len(line) + 1
        if take_start:
            start.append((number, line))
            low += 1
        else:
            end.append((number, line))
            high -= 1
        take_start = not take_start
    kept = start + list(reversed(end))
    omitted = len(numbered) - len(kept)
    lines = [header, *(line for _, line in start)]
    if omitted:
        lines.append(f"... {omitted} lines in the middle of the chat are left out ...")
    lines += [line for _, line in reversed(end)]
    lines.append(footer)
    refs = {number: entries[number - 1].ref for number, _ in kept}
    return Digest("\n".join(line for line in lines if line), refs)


def _header(cursor: pg8000.dbapi.Cursor, session_id: str) -> str:
    cursor.execute(_SESSION, (session_id,))
    row = cursor.fetchone()
    if row is None:
        return f"CHAT {session_id[:8]}"
    title, started, ended, branch = row
    parts = [f"CHAT {session_id[:8]}"]
    if title:
        parts.append(f"title: {_shorten(str(title), 120)}")
    if started:
        parts.append(f"started: {started:%Y-%m-%d %H:%M}")
    if ended:
        parts.append(f"ended: {ended:%Y-%m-%d %H:%M}")
    if branch:
        parts.append(f"branch: {_shorten(str(branch), 60)}")
    return "; ".join(parts)


def _footer(cursor: pg8000.dbapi.Cursor, session_id: str) -> str:
    cursor.execute(_COMMITS, (session_id,))
    commits = []
    for sha, committed_at, confidence, matched, total in cursor.fetchall():
        when = f" {committed_at:%Y-%m-%d}" if committed_at else ""
        commits.append(
            f"COMMIT {str(sha)[:40]}{when} (explained {matched}/{total} lines, "
            f"confidence {confidence:.2f})"
        )
    return "\n".join(commits)


def _entries(cursor: pg8000.dbapi.Cursor, session_id: str) -> list[_Entry]:
    entries: list[_Entry] = []
    cursor.execute(_MESSAGES, (session_id,))
    for event_id, event_type, at, key, line, text in cursor.fetchall():
        if not text:
            continue
        is_prompt = event_type == "prompt"
        ref = LineRef(event_id, key, line, "prompt" if is_prompt else "reply")
        entries.append(_Entry(_when(at), ref, "PROMPT" if is_prompt else "REPLY", str(text)))
    cursor.execute(_COMMANDS, (session_id,))
    for event_id, at, key, line, name, arguments in cursor.fetchall():
        ref = LineRef(event_id, key, line, "command")
        entries.append(
            _Entry(_when(at), ref, "COMMAND", detail=f"{name or '?'} {arguments or ''}".strip())
        )
    cursor.execute(_EDITS, (session_id,))
    for row in cursor.fetchall():
        event_id, at, key, line, path, count, added, removed, role, created, deleted = row
        change = "created" if created else ("deleted" if deleted else "modified")
        ref = LineRef(event_id, key, line, "edit", (FileRef(str(path), change, "edit"),))
        role_text = f", {role}" if role else ""
        detail = f"{path} ({change}, {count} edits, +{added}/-{removed}{role_text})"
        entries.append(_Entry(_when(at), ref, "EDIT", detail=detail))
    cursor.execute(_SHELL, (session_id,))
    for event_id, at, key, line, commands in cursor.fetchall():
        for command in commands or []:  # one digest line per command: a decision cites only its own
            files = tuple(
                FileRef(item.path, item.change, "shell") for item in files_changed_by(command or "")
            )
            if not files:
                continue
            ref = LineRef(event_id, key, line, "shell", files)
            shown = "; ".join(f"{item.change} {item.path}" for item in files[:12])
            more = f"; and {len(files) - 12} more" if len(files) > 12 else ""
            entries.append(_Entry(_when(at), ref, "SHELL", detail=f"by command: {shown}{more}"))
    cursor.execute(_THINKING, (session_id,))
    for event_id, at, key, line, text in cursor.fetchall():
        note = " ".join(str(text).split())[:_NOTE_CHARS]
        ref = LineRef(event_id, key, line, "think", note=note)
        entries.append(_Entry(_when(at), ref, "THINK", excerpt=note))
    cursor.execute(_TESTS, (session_id,))
    for row in cursor.fetchall():
        event_id, at, key, line, framework, passed, n_passed, n_failed, n_skipped, command = row
        outcome = {True: "PASSED", False: "FAILED"}.get(passed, "UNKNOWN")
        counts = ""
        if n_passed is not None or n_failed is not None:
            counts = f" ({n_passed or 0} passed, {n_failed or 0} failed, {n_skipped or 0} skipped)"
        ref = LineRef(event_id, key, line, "test")
        detail = f"{framework} {outcome}{counts} `{_shorten(str(command), 80)}`"
        entries.append(_Entry(_when(at), ref, "TEST", detail=detail))
    cursor.execute(_CORRECTIONS, (session_id,))
    for event_id, at, key, line, kind, corrected_by, note in cursor.fetchall():
        ref = LineRef(event_id, key, line, "correction")
        detail = f"{kind} (put right by {corrected_by})"
        entries.append(_Entry(_when(at), ref, "CORRECTION", excerpt=note or "", detail=detail))
    return entries


def _when(at: Any) -> datetime:
    return at if isinstance(at, datetime) else _EARLIEST


def _shorten(text: str, width: int) -> str:
    """One line of at most ``width`` characters (white space collapsed, ``...`` if cut)."""
    flat = " ".join(text.split())
    if len(flat) <= width:
        return flat
    return flat[: max(width - 3, 0)] + "..."
