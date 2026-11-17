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
from ccp.decisions.prompts import PROMPT_VERSION

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
# The last time the chat was handled: its newest decision, or the last run that found none.
_NEWEST_DECISION = (
    "GREATEST((SELECT max(d.created_at) FROM decision d WHERE d.session_id = s.session_id), "
    "s.decisions_checked_at)"
)

# Whether the chat was last looked at with an older prompt version (informational: it does not make
# a chat pending, to save tokens).
_OUTDATED = "s.decisions_checked_version IS DISTINCT FROM %s"

_STATE = f"""
    SELECT s.is_empty, {_HAS_PROMPT}, {_LAST_EVENT}, {_NEWEST_DECISION}, {_OUTDATED}
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
                          decided_by, files, thinking, llm_usage, kind, raised_by, outcome,
                          follows_session_id, follows_prompt_uuid, details)
    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::text[], %s::jsonb, %s, 'ai', %s, %s,
            %s::jsonb, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb)
"""

# A decision that follows an earlier decision of the same chat: the link is the earlier decision's
# prompt (stable), found once the prompts are filled in.
_LINK_SAME_CHAT = """
    UPDATE decision f SET follows_session_id = f.session_id, follows_prompt_uuid = a.prompt_uuid
      FROM decision a
     WHERE f.session_id = %s AND a.session_id = f.session_id AND a.prompt_uuid IS NOT NULL
       AND f.follows_session_id IS NULL AND a.seq = (f.details ->> 'follows_seq')::int
       AND a.decision_id <> f.decision_id
"""

# The outcome of open advice (waiting or postponed) is what the newest decision that follows it
# says; without one, it is what the advice itself said. Other decisions keep their own outcome.
_RELINK = """
    WITH want AS (
        SELECT d.decision_id,
               COALESCE((SELECT f.details ->> 'follows_how' FROM decision f
                          WHERE f.follows_session_id = d.session_id
                            AND f.follows_prompt_uuid = d.prompt_uuid
                            AND f.decision_id <> d.decision_id
                            AND f.details ->> 'follows_how' IS NOT NULL
                          ORDER BY f.created_at DESC, f.decision_id DESC LIMIT 1),
                        d.details ->> 'own_outcome') AS outcome
          FROM decision d
         WHERE d.details ->> 'own_outcome' IN ('waiting', 'postponed')  -- only open advice
    )
    UPDATE decision d SET outcome = w.outcome
      FROM want w WHERE d.decision_id = w.decision_id AND d.outcome IS DISTINCT FROM w.outcome
"""
# The models that did the work of each decision, with their effort level: every response from the
# developer's prompt to the end of the turn of the decision's last evidence line (all models, not
# only the first one).
_SET_MODELS = """
    UPDATE decision d SET details = d.details || jsonb_build_object('models_used', COALESCE((
        SELECT jsonb_agg(jsonb_build_object('model', m.model, 'effort', m.effort,
                                            'responses', m.n) ORDER BY m.model, m.effort)
          FROM (
            SELECT u.model, u.effort, count(*) AS n
              FROM (SELECT p.raw_object_key AS sk, p.raw_line_number AS sl FROM event p
                     WHERE p.session_id = d.session_id AND p.entry_uuid = d.prompt_uuid
                     ORDER BY p.event_id LIMIT 1) st,
                   (SELECT x ->> 'key' AS ek, (x ->> 'line')::int AS el
                      FROM jsonb_array_elements(d.evidence_lines) x
                     ORDER BY 1 DESC, 2 DESC LIMIT 1) en
                   LEFT JOIN LATERAL (
                     SELECT n.raw_object_key AS nk, n.raw_line_number AS nl
                       FROM event n
                      WHERE n.session_id = d.session_id AND n.event_type = 'prompt'
                        AND n.actor = 'human' AND NOT n.is_abandoned
                        AND split_part(n.raw_object_key, '/', 3) = 'main'
                        AND (n.raw_object_key, n.raw_line_number) > (en.ek, en.el)
                      ORDER BY n.raw_object_key ASC, n.raw_line_number ASC LIMIT 1
                   ) nxt ON true,
                   model_usage u JOIN event e ON e.event_id = u.event_id
             WHERE e.session_id = d.session_id AND NOT e.is_abandoned
               AND split_part(e.raw_object_key, '/', 3) = 'main'
               AND u.model IS NOT NULL AND u.model <> '<synthetic>'
               AND (e.raw_object_key, e.raw_line_number) >= (st.sk, st.sl)
               AND (nxt.nk IS NULL OR (e.raw_object_key, e.raw_line_number) < (nxt.nk, nxt.nl))
             GROUP BY u.model, u.effort) m), '[]'::jsonb))
     WHERE d.session_id = %s
"""
_SET_VERSION = "UPDATE session SET decisions_checked_version = %s WHERE session_id = %s"
_DELETE_AI_CORRECTIONS = """
    DELETE FROM correction
     WHERE session_id = %s AND kind = 'developer_correction' AND detected_by = 'ai'
"""
_INSERT_CORRECTION = """
    INSERT INTO correction (session_id, chat_session_id, kind, corrected_by, trigger_event_id,
                            detected_by, note)
    SELECT %s, (SELECT s.chat_session_id FROM chat_session s
                 JOIN event t ON t.event_id = %s
                WHERE s.session_id = t.session_id AND s.started_at <= t.occurred_at
                ORDER BY s.started_at DESC, s.seq DESC LIMIT 1),
           'developer_correction', 'developer', %s, 'ai', %s
    ON CONFLICT (session_id, kind, trigger_event_id) DO UPDATE
        SET note = EXCLUDED.note, detected_by = 'ai', corrected_by = 'developer',
            chat_session_id = EXCLUDED.chat_session_id
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
    outdated: bool = False  # last looked at with an older prompt version

    def needs_decisions(self) -> bool:
        """True without decisions, or when the chat has events newer than the newest decision.

        A newer prompt version alone does not make an old chat pending: it costs tokens, so a chat
        is rewritten with the new prompt only when it has new activity (or is forced).
        """
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
    thinking: list[LineRef] = field(default_factory=list)
    kind: str = "code change"
    raised_by: str | None = None
    outcome: str = "done"
    follows_session_id: str | None = None  # earlier advice in another chat ...
    follows_prompt_uuid: str | None = None  # ... found by the prompt it answered
    follows_local: int | None = None  # or the 1-based number of an earlier decision of this chat
    details: dict[str, Any] = field(default_factory=dict)  # short answer, use when, quotes ...


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
    cursor.execute(_STATE, (PROMPT_VERSION, session_id))
    row = cursor.fetchone()
    connection.rollback()
    if row is None:
        return None
    return ChatState(bool(row[0]), bool(row[1]), row[2], row[3], bool(row[4]))


def pending_chats(connection: pg8000.dbapi.Connection, force: bool) -> list[str]:
    """Chats to process, most recently active first (every non-empty chat with ``force``)."""
    cursor = connection.cursor()
    cursor.execute(_PENDING, (force,))
    chats = [row[0] for row in cursor.fetchall()]
    connection.rollback()
    return chats


def mark_checked(connection: pg8000.dbapi.Connection, session_id: str) -> None:
    """Remember that the chat was given to the decisions run (also when it found no decision)."""
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE session SET decisions_checked_at = now(), decisions_checked_version = %s "
        "WHERE session_id = %s",
        (PROMPT_VERSION, session_id),
    )
    connection.commit()


def linked_commits(connection: pg8000.dbapi.Connection, session_id: str) -> list[str]:
    """The full ids of the commits linked to a chat."""
    cursor = connection.cursor()
    cursor.execute(_COMMITS, (session_id,))
    shas = [row[0] for row in cursor.fetchall()]
    connection.rollback()
    return shas


# The developer prompt current at each decision's first evidence line, stored with it.
_SET_PROMPTS = """
    UPDATE decision d SET
        prompt_text = CASE WHEN jsonb_typeof(pe.payload #> '{message,content}') = 'string'
                           THEN pe.payload #>> '{message,content}'
                           ELSE (SELECT string_agg(b ->> 'text', E'\n\n')
                                   FROM jsonb_array_elements(pe.payload #> '{message,content}') b
                                  WHERE b ->> 'type' = 'text') END,
        prompt_at = pe.occurred_at, prompt_uuid = pe.entry_uuid
      FROM (
        SELECT d2.decision_id,
               (SELECT e.event_id FROM event e
                 WHERE e.session_id = d2.session_id AND e.event_type = 'prompt'
                   AND e.actor = 'human' AND NOT e.is_abandoned
                   AND split_part(e.raw_object_key, '/', 3) = 'main'
                   AND (e.raw_object_key, e.raw_line_number) <= (first_ev.key, first_ev.line)
                 ORDER BY e.raw_object_key DESC, e.raw_line_number DESC LIMIT 1) AS event_id
          FROM decision d2
          CROSS JOIN LATERAL (
            SELECT x ->> 'key' AS key, (x ->> 'line')::int AS line
              FROM jsonb_array_elements(d2.evidence_lines) x
             WHERE split_part(x ->> 'key', '/', 3) = 'main'
             ORDER BY x ->> 'key', (x ->> 'line')::int LIMIT 1) first_ev
         WHERE d2.session_id = %s AND (%s OR d2.prompt_text IS NULL)) q
      JOIN event pe ON pe.event_id = q.event_id
     WHERE d.decision_id = q.decision_id
"""


def fill_missing_prompts(connection: pg8000.dbapi.Connection) -> int:
    """Store the prompt of every decision that has none yet; returns how many decisions got one."""
    cursor = connection.cursor()
    cursor.execute("SELECT DISTINCT session_id FROM decision WHERE prompt_text IS NULL")
    sessions = [row[0] for row in cursor.fetchall()]
    done = 0
    for session_id in sessions:
        cursor.execute(_SET_PROMPTS, (session_id, False))
        done += max(cursor.rowcount, 0)
    connection.commit()
    return done


def save_decisions(
    connection: pg8000.dbapi.Connection,
    session_id: str,
    decisions: list[NewDecision],
    developer_corrections: list[tuple[int, str]],
    model: str,
    prompt_version: str,
    review_model: str | None = None,
    llm_usage: dict[str, Any] | None = None,
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
        base = seq
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
            details = dict(decision.details)
            if decision.follows_local:
                details["follows_seq"] = base + decision.follows_local
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
                    json.dumps(
                        [
                            {
                                "text": ref.note,
                                "key": ref.raw_object_key,
                                "line": ref.raw_line_number,
                            }
                            for ref in decision.thinking
                        ],
                        ensure_ascii=False,
                    ),
                    json.dumps(llm_usage or {}),
                    decision.kind,
                    decision.raised_by,
                    decision.outcome,
                    decision.follows_session_id,
                    decision.follows_prompt_uuid,
                    json.dumps(details, ensure_ascii=False),
                ),
            )
        cursor.execute(_DELETE_AI_CORRECTIONS, (session_id,))
        for event_id, note in developer_corrections:
            cursor.execute(_INSERT_CORRECTION, (session_id, event_id, event_id, note))
        cursor.execute(_SET_PROMPTS, (session_id, True))  # the prompt each decision answers
        cursor.execute(_SET_MODELS, (session_id,))
        cursor.execute(_LINK_SAME_CHAT, (session_id,))
        cursor.execute(_RELINK)  # advice takes the outcome its follower reports
        cursor.execute(_SET_VERSION, (PROMPT_VERSION, session_id))
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
