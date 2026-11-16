"""The database side of decisions: what is pending, saving, listing and the human override.

The ``decision`` table is not index data: a rebuild never deletes it. Saving the decisions of a chat
replaces the ones a model wrote earlier but never the ones a person reviewed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pg8000.dbapi

from ccp.decisions.digest import FileRef, LineRef

HUMAN_STATUSES = ("approved", "rejected", "needs_review")
MAX_REVIEWER = 100
MAX_HUMAN_NOTE = 1000

# A chat can have decisions when it has a message from the developer (an empty chat has none).
_HAS_PROMPT = """
    EXISTS (SELECT 1 FROM event p WHERE p.session_id = s.session_id AND p.event_type = 'prompt'
                                        AND p.actor = 'human' AND NOT p.is_abandoned)
"""
_LAST_EVENT = """
    (SELECT max(e.occurred_at) FROM event e
      WHERE e.session_id = s.session_id AND NOT e.is_abandoned)
"""
_NEWEST_DECISION = "(SELECT max(d.created_at) FROM decision d WHERE d.session_id = s.session_id)"

_STATE = f"""
    SELECT s.is_empty, {_HAS_PROMPT}, {_LAST_EVENT}, {_NEWEST_DECISION}
      FROM session s WHERE s.session_id = %s
"""  # noqa: S608  (only constants are inserted)

# Chats to process: not empty, and without decisions or with events newer than the newest decision.
_PENDING = f"""
    SELECT s.session_id FROM session s
     WHERE NOT s.is_empty AND {_HAS_PROMPT}
       AND (%s OR {_NEWEST_DECISION} IS NULL OR {_LAST_EVENT} > {_NEWEST_DECISION})
     ORDER BY s.last_activity_at DESC, s.session_id
"""  # noqa: S608  (only constants are inserted)

_FIND_CHATS = "SELECT session_id FROM session WHERE session_id = %s OR starts_with(session_id, %s)"

_DELETE_AI_DECISIONS = """
    DELETE FROM decision WHERE session_id = %s AND reviewed_by_kind IS DISTINCT FROM 'human'
"""
_NEXT_SEQ = "SELECT COALESCE(max(seq), 0) FROM decision WHERE session_id = %s"
_INSERT_DECISION = """
    INSERT INTO decision (session_id, seq, title, goal, options, chosen, reason, result,
                          tests_summary, commit_shas, evidence_lines, status, reviewed_by_kind,
                          reviewer, review_note, review_history, model, prompt_version,
                          decided_by, files)
    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::text[], %s::jsonb, %s, 'ai', %s, %s,
            %s::jsonb, %s, %s, %s, %s::jsonb)
"""
_DELETE_AI_CORRECTIONS = """
    DELETE FROM correction
     WHERE session_id = %s AND kind = 'developer_correction' AND detected_by = 'ai'
"""
_INSERT_CORRECTION = """
    INSERT INTO correction (session_id, kind, corrected_by, trigger_event_id, detected_by, note)
    VALUES (%s, 'developer_correction', 'developer', %s, 'ai', %s)
    ON CONFLICT (session_id, kind, trigger_event_id) DO UPDATE
        SET note = EXCLUDED.note, detected_by = 'ai', corrected_by = 'developer'
"""
_COMMITS = "SELECT commit_sha FROM commit_link WHERE session_id = %s ORDER BY commit_sha"

_LIST = """
    SELECT decision_id, seq, title, status, reviewed_by_kind, reviewer, review_note, model,
           jsonb_array_length(evidence_lines), jsonb_array_length(review_history)
      FROM decision WHERE session_id = %s ORDER BY seq
"""

_SET_STATUS = """
    UPDATE decision
       SET status = %s, reviewed_by_kind = 'human', reviewer = %s, review_note = %s,
           review_history = review_history || %s::jsonb, updated_at = now()
     WHERE decision_id = %s
"""


class DecisionError(RuntimeError):
    """A decision operation failed. The message is safe to show."""


@dataclass(frozen=True)
class ChatState:
    """What decides whether a chat needs its decisions (re)made."""

    is_empty: bool
    has_prompt: bool
    last_event_at: datetime | None
    newest_decision_at: datetime | None

    def needs_decisions(self) -> bool:
        """True without decisions, or when the chat has events newer than the newest decision."""
        if self.newest_decision_at is None:
            return True
        return self.last_event_at is not None and self.last_event_at > self.newest_decision_at


@dataclass(frozen=True)
class NewDecision:
    """A decision with its AI review, ready to store."""

    title: str
    goal: str
    options: list[str]
    chosen: str
    reason: str
    result: str
    tests_summary: str | None
    commit_shas: list[str]
    evidence: list[LineRef]
    status: str  # approved | needs_review
    review_note: str
    decided_by: str | None = None  # developer | claude
    files: list[FileRef] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionRow:
    """A stored decision, as ``list`` shows it (counts and short text only)."""

    decision_id: int
    seq: int
    title: str | None
    status: str
    reviewed_by_kind: str | None
    reviewer: str | None
    review_note: str | None
    model: str | None
    evidence_count: int
    history_count: int


def find_chats(connection: pg8000.dbapi.Connection, text: str) -> list[str]:
    """Chat ids equal to ``text`` or starting with it."""
    cursor = connection.cursor()
    cursor.execute(_FIND_CHATS, (text, text))
    found = [row[0] for row in cursor.fetchall()]
    return [text] if text in found else found


def chat_state(connection: pg8000.dbapi.Connection, session_id: str) -> ChatState | None:
    """The facts that decide whether to process a chat; None if there is no such chat."""
    cursor = connection.cursor()
    cursor.execute(_STATE, (session_id,))
    row = cursor.fetchone()
    connection.rollback()
    if row is None:
        return None
    return ChatState(bool(row[0]), bool(row[1]), row[2], row[3])


def pending_chats(connection: pg8000.dbapi.Connection, force: bool) -> list[str]:
    """Chats to process, most recently active first (every non-empty chat with ``force``)."""
    cursor = connection.cursor()
    cursor.execute(_PENDING, (force,))
    chats = [row[0] for row in cursor.fetchall()]
    connection.rollback()
    return chats


def linked_commits(connection: pg8000.dbapi.Connection, session_id: str) -> list[str]:
    """The full ids of the commits linked to a chat."""
    cursor = connection.cursor()
    cursor.execute(_COMMITS, (session_id,))
    shas = [row[0] for row in cursor.fetchall()]
    connection.rollback()
    return shas


def save_decisions(
    connection: pg8000.dbapi.Connection,
    session_id: str,
    decisions: list[NewDecision],
    developer_corrections: list[tuple[int, str]],
    model: str,
    prompt_version: str,
    review_model: str | None = None,
) -> None:
    """Replace the chat's AI-written decisions and developer corrections, in one transaction.

    Decisions a person reviewed are kept (new ones are numbered after them). Developer corrections
    are ``(trigger event id, note)`` pairs. ``model`` wrote the decisions and ``review_model``
    (default: the same) reviewed them.
    """
    cursor = connection.cursor()
    try:
        cursor.execute(_DELETE_AI_DECISIONS, (session_id,))
        cursor.execute(_NEXT_SEQ, (session_id,))
        seq = int(cursor.fetchone()[0])
        now = datetime.now(UTC).isoformat()
        reviewer = review_model or model
        for decision in decisions:
            seq += 1
            history = [
                {
                    "at": now,
                    "by_kind": "ai",
                    "reviewer": reviewer,
                    "status": decision.status,
                    "note": decision.review_note,
                }
            ]
            evidence = [
                {"key": ref.raw_object_key, "line": ref.raw_line_number}
                for ref in decision.evidence
            ]
            cursor.execute(
                _INSERT_DECISION,
                (
                    session_id,
                    seq,
                    decision.title,
                    decision.goal,
                    json.dumps(decision.options, ensure_ascii=False),
                    decision.chosen,
                    decision.reason,
                    decision.result,
                    decision.tests_summary,
                    decision.commit_shas,
                    json.dumps(evidence),
                    decision.status,
                    reviewer,
                    decision.review_note,
                    json.dumps(history, ensure_ascii=False),
                    model,
                    prompt_version,
                    decision.decided_by,
                    json.dumps(
                        [
                            {"path": item.path, "change": item.change, "via": item.via}
                            for item in decision.files
                        ]
                    ),
                ),
            )
        cursor.execute(_DELETE_AI_CORRECTIONS, (session_id,))
        for event_id, note in developer_corrections:
            cursor.execute(_INSERT_CORRECTION, (session_id, event_id, note))
        connection.commit()
    except pg8000.dbapi.DatabaseError as error:
        connection.rollback()
        raise DecisionError(f"saving the decisions failed: {type(error).__name__}") from error


def list_decisions(connection: pg8000.dbapi.Connection, session_id: str) -> list[DecisionRow]:
    """The decisions of a chat in order."""
    cursor = connection.cursor()
    cursor.execute(_LIST, (session_id,))
    rows = [DecisionRow(*row) for row in cursor.fetchall()]
    connection.rollback()
    return rows


def set_decision_status(
    connection: pg8000.dbapi.Connection,
    decision_id: int,
    status: str,
    reviewer: str,
    note: str | None = None,
) -> None:
    """A person approves, rejects or flags a decision.

    The decision becomes ``reviewed_by_kind = 'human'`` and the review is appended to
    ``review_history`` (the list is only ever added to). Later AI runs keep it.

    Args:
        connection: An open connection; the change is committed.
        decision_id: The decision.
        status: ``approved``, ``rejected`` or ``needs_review``.
        reviewer: Who reviewed (a name or e-mail; 1 to 100 characters).
        note: An optional comment (up to 1000 characters).

    Raises:
        DecisionError: The status or reviewer is invalid, the note is too long, or there is no
            such decision.
    """
    if status not in HUMAN_STATUSES:
        raise DecisionError(f"status must be one of: {', '.join(HUMAN_STATUSES)}")
    name = reviewer.strip()
    if not name or len(name) > MAX_REVIEWER:
        raise DecisionError(f"the reviewer must be 1 to {MAX_REVIEWER} characters")
    comment = (note or "").strip()
    if len(comment) > MAX_HUMAN_NOTE:
        raise DecisionError(f"the note must be at most {MAX_HUMAN_NOTE} characters")
    entry: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "by_kind": "human",
        "reviewer": name,
        "status": status,
        "note": comment,
    }
    cursor = connection.cursor()
    try:
        cursor.execute(
            _SET_STATUS,
            (status, name, comment or None, json.dumps([entry], ensure_ascii=False), decision_id),
        )
        updated = cursor.rowcount
        if updated != 1:
            connection.rollback()
            raise DecisionError(f"there is no decision {decision_id}")
        connection.commit()
    except pg8000.dbapi.DatabaseError as error:
        connection.rollback()
        raise DecisionError(f"updating the decision failed: {type(error).__name__}") from error
