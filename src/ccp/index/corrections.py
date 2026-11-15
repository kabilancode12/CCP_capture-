"""Corrections found by rule: something had to be put right (table ``correction``).

Five kinds, all with ``detected_by = 'rule'`` (the distillation adds ``developer_correction`` rows
with ``detected_by = 'ai'``; they are never touched here):

=================  ==============  =============================================================
kind               corrected_by    rule
=================  ==============  =============================================================
``test_fix``       test            a failed test run, then edits, then a passing run of the same
                                   runner (the logic of the view ``v_test_fix_cycle``). Trigger:
                                   the failed run's call; fix: the passing run's call.
``command_retry``  claude          a shell command failed (error with an exit code) and, within the
                                   next 5 tool calls of the same transcript, a shell command with
                                   the same first program ran. Trigger: the failed result; fix:
                                   the retrying call.
``rework``         claude          an edit changed lines that an earlier AI edit of the same file
                                   (at most 30 minutes before) had changed. Trigger: the earlier
                                   edit's result; fix: the later edit's result. An earlier edit
                                   counts once, for the first edit that redid it.
``rewind``         developer       the developer went back and typed another prompt (the view
                                   ``v_human_decision``). Trigger: the abandoned prompt; fix: the
                                   prompt that replaced it.
``refused``        developer       the developer refused a tool call (``v_human_decision``).
                                   Trigger: the refused call's result.
=================  ==============  =============================================================

Work on abandoned branches (rewound work) is not counted, except the rewind itself. The rules are
simple on purpose: a correction is a pointer to two events, which a person can follow.

The detection rules are plain functions of lists (:func:`first_program`,
:func:`find_command_retries`,
:func:`find_reworks`); :func:`refresh_corrections` reads the events, runs them and stores the rows.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ccp.index.sessions import CHAT_SESSION_OF_EVENT
from ccp.index.testruns import exit_code_of

RETRY_WINDOW_CALLS = 5
REWORK_WINDOW = timedelta(minutes=30)
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_SETUP_COMMANDS = frozenset({"cd", "pushd", "export", "set", "source", "."})


@dataclass(frozen=True)
class ToolCall:
    """A tool call of a transcript, with how its result turned out.

    Attributes:
        event_id: The call's event.
        stream: The transcript it is in (``main`` or ``agent-<id>``): calls are counted per stream.
        tool: The tool's name (``Bash``, ``Edit`` ...).
        command: The shell command, for a shell tool.
        result_event_id: The event of the call's result, if it has one.
        failed: The result was an error with a non-zero exit code (a refused call is not a failure).
    """

    event_id: int
    stream: str
    tool: str | None
    command: str | None
    result_event_id: int | None
    failed: bool


@dataclass(frozen=True)
class Edit:
    """One AI edit of a file (a ``file_change``)."""

    event_id: int
    path: str
    at: datetime
    hunks: Sequence[dict[str, int]]


@dataclass(frozen=True)
class Found:
    """A correction ready to store."""

    kind: str
    corrected_by: str
    trigger_event_id: int
    fix_event_id: int | None
    note: str | None


def first_program(command: str | None) -> str | None:
    """The program a shell command starts: ``cd app && CI=1 python -m pytest`` gives ``python``.

    Leading ``cd`` and ``export`` parts and ``NAME=value`` prefixes are skipped; the program is
    named without its folder and without ``.exe``, in lower case.
    """
    for segment in _SEGMENT_SPLIT.split(command or ""):
        tokens = [token for token in segment.split() if not _ASSIGNMENT.match(token)]
        if not tokens or tokens[0] in _SETUP_COMMANDS:
            continue
        name = re.split(r"[\\/]", tokens[0].strip("'\""))[-1].lower()
        return name.removesuffix(".exe") or None
    return None


def find_command_retries(calls: Sequence[ToolCall]) -> list[Found]:
    """Find failed shell commands that were run again (see the module description).

    Args:
        calls: Every tool call of the chat, in transcript order, one stream after the other.
    """
    found = []
    for index, call in enumerate(calls):
        program = first_program(call.command) if call.tool in SHELL_TOOLS else None
        if not call.failed or program is None or call.result_event_id is None:
            continue
        for later in calls[index + 1 : index + 1 + RETRY_WINDOW_CALLS]:
            if later.stream != call.stream:
                break
            if later.tool in SHELL_TOOLS and first_program(later.command) == program:
                found.append(
                    Found(
                        "command_retry",
                        "claude",
                        call.result_event_id,
                        later.event_id,
                        f"{program} failed, run again",
                    )
                )
                break
    return found


def _overlaps(first: tuple[int, int], second: tuple[int, int]) -> bool:
    """Whether two ``(start, length)`` line ranges share a line (a length of 0 counts as 1)."""
    start_a, end_a = first[0], first[0] + max(first[1], 1)
    start_b, end_b = second[0], second[0] + max(second[1], 1)
    return start_a < end_b and start_b < end_a


def _touches(earlier: Edit, later: Edit) -> bool:
    """Whether the later edit changed lines the earlier one wrote (new lines against old lines)."""
    written = [(h["new_start"], h["new_lines"]) for h in earlier.hunks]
    replaced = [(h["old_start"], h["old_lines"]) for h in later.hunks]
    return any(_overlaps(a, b) for a in written for b in replaced)


def find_reworks(edits: Sequence[Edit]) -> list[Found]:
    """Find edits that redid lines an earlier edit of the same file had changed.

    Args:
        edits: The AI edits of the chat (not the abandoned ones).
    """
    ordered = sorted(edits, key=lambda edit: (edit.at, edit.event_id))
    found: list[Found] = []
    taken: set[int] = set()
    for index, later in enumerate(ordered):
        for earlier in reversed(ordered[:index]):
            if later.at - earlier.at > REWORK_WINDOW:
                break
            if earlier.path == later.path and _touches(earlier, later):
                if earlier.event_id not in taken:
                    taken.add(earlier.event_id)
                    found.append(
                        Found(
                            "rework",
                            "claude",
                            earlier.event_id,
                            later.event_id,
                            "same lines edited again",
                        )
                    )
                break
    return found


# A failed test run that edits and a passing run followed (the view's own rows).
_TEST_FIXES = """
    SELECT fr.call_event_id, pr.call_event_id, c.framework, c.edits_between
      FROM v_test_fix_cycle c
      JOIN test_run fr ON fr.test_run_id = c.failed_run_id
      JOIN test_run pr ON pr.test_run_id = c.passed_run_id
      JOIN event e ON e.event_id = fr.call_event_id
     WHERE c.session_id = %s AND c.edits_between > 0 AND NOT e.is_abandoned
     ORDER BY fr.call_event_id
"""

# Every tool call (the first tool_use of its entry) with the result that answered it. A result is a
# failure when it is an error that starts with "Exit code <n>" and was neither refused nor
# interrupted. The command is cut: only its first words matter.
_TOOL_CALLS = """
    SELECT c.event_id, split_part(c.raw_object_key, '/', 3), u.block ->> 'name',
           left(u.block #>> '{input,command}', 1000), r.event_id,
           r.event_id IS NOT NULL
               AND jsonb_path_exists(r.payload,
                   '$.message.content[*] ? (@.type == "tool_result" && @.is_error == true)')
               AND NOT (r.payload ? 'toolDenialKind')
               AND COALESCE(r.payload #>> '{toolUseResult,interrupted}', 'false') <> 'true',
           substring(r.payload #>> '{message,content,0,content}' FROM '^Exit code [0-9]+')
      FROM event c
      CROSS JOIN LATERAL (
          SELECT jsonb_path_query_first(
                     c.payload, '$.message.content[*] ? (@.type == "tool_use")') AS block) u
      LEFT JOIN LATERAL (
          SELECT x.event_id, x.payload FROM event x
           WHERE x.session_id = c.session_id AND x.event_type = 'tool_result'
             AND x.tool_use_ids && c.tool_use_ids
           ORDER BY x.event_id LIMIT 1) r ON TRUE
     WHERE c.session_id = %s AND c.event_type = 'tool_call' AND NOT c.is_abandoned
     ORDER BY split_part(c.raw_object_key, '/', 3), c.raw_object_key, c.raw_line_number
"""

_EDITS = """
    SELECT f.event_id, f.path, e.occurred_at, f.hunks
      FROM file_change f JOIN event e ON e.event_id = f.event_id
     WHERE f.session_id = %s AND NOT e.is_abandoned AND e.occurred_at IS NOT NULL
"""

# Rewinds (with the prompt that replaced the abandoned one, found as the view does) and refusals.
_HUMAN_DECISIONS = """
    SELECT h.decision_type, h.event_id, h.detail, h.tool,
           CASE WHEN h.decision_type = 'rewind' THEN
               (SELECT s.event_id FROM event e
                  JOIN event s ON s.session_id = e.session_id AND s.parent_uuid = e.parent_uuid
                              AND s.event_type = 'prompt' AND s.actor = 'human'
                              AND NOT s.is_abandoned
                 WHERE e.event_id = h.event_id ORDER BY s.occurred_at, s.event_id LIMIT 1) END
      FROM v_human_decision h
     WHERE h.session_id = %s AND h.decision_type IN ('rewind', 'denial')
     ORDER BY h.event_id
"""

_DELETE_RULE_CORRECTIONS = "DELETE FROM correction WHERE session_id = %s AND detected_by = 'rule'"

# The session is the one the trigger event happened in (see CHAT_SESSION_OF_EVENT). A row of the
# distillation (detected_by 'ai') for the same kind and event stays and wins.
_INSERT_CORRECTION_HEAD = """
    INSERT INTO correction (session_id, chat_session_id, kind, corrected_by, trigger_event_id,
                            fix_event_id, detected_by, note)
    SELECT e.session_id, cs.chat_session_id, %s, %s, e.event_id, %s, 'rule', %s
      FROM event e
      LEFT JOIN LATERAL ("""
_INSERT_CORRECTION_TAIL = """) cs ON TRUE
     WHERE e.event_id = %s
    ON CONFLICT (session_id, kind, trigger_event_id) DO NOTHING
"""
_INSERT_CORRECTION = _INSERT_CORRECTION_HEAD + CHAT_SESSION_OF_EVENT + _INSERT_CORRECTION_TAIL


def refresh_corrections(cursor: Any, session_id: str) -> None:
    """Find the chat's corrections by rule and store them, replacing the earlier rule rows.

    Part of the indexer's refresh, in its transaction, after the test runs and sessions are
    current. Rows found by the distillation (``detected_by = 'ai'``) are left alone.

    Args:
        cursor: A cursor of the refresh's connection.
        session_id: The chat.
    """
    cursor.execute(_DELETE_RULE_CORRECTIONS, (session_id,))
    found: list[Found] = []

    cursor.execute(_TEST_FIXES, (session_id,))
    for failed_call, passed_call, framework, edits in cursor.fetchall():
        note = f"{framework} failed, {edits} edit(s), then passed"
        found.append(Found("test_fix", "test", failed_call, passed_call, note))

    cursor.execute(_TOOL_CALLS, (session_id,))
    calls = [
        ToolCall(
            event_id=row[0],
            stream=row[1],
            tool=row[2],
            command=row[3],
            result_event_id=row[4],
            failed=bool(row[5]) and (exit_code_of(row[6]) or 0) != 0,
        )
        for row in cursor.fetchall()
    ]
    found.extend(find_command_retries(calls))

    cursor.execute(_EDITS, (session_id,))
    found.extend(
        find_reworks([Edit(row[0], row[1], row[2], row[3] or []) for row in cursor.fetchall()])
    )

    cursor.execute(_HUMAN_DECISIONS, (session_id,))
    for decision, event_id, detail, tool, replacement in cursor.fetchall():
        if decision == "rewind":
            found.append(Found("rewind", "developer", event_id, replacement, "prompt replaced"))
        else:
            note = "refused" + (f" {tool}" if tool else "") + (f" ({detail})" if detail else "")
            found.append(Found("refused", "developer", event_id, None, note))

    for item in found:
        cursor.execute(
            _INSERT_CORRECTION,
            (item.kind, item.corrected_by, item.fix_event_id, item.note, item.trigger_event_id),
        )
