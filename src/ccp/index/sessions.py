"""The sessions of a chat: when it was opened, and for how long (table ``chat_session``).

The capture client reports each opening itself (SessionStart / SessionEnd hooks); those rows carry a
``run_key`` and are never touched by the indexer. A chat captured before that, or whose hooks never
ran, has none, so the indexer **infers** its sessions from the main transcript's time stamps: a new
session starts at the first line, and again after any gap longer than ``CCP_SESSION_IDLE_MINUTES``
(default 30, the same limit that closes an idle chat) between two lines. Inferred rows are marked
``start_source = 'inferred'``, ``end_reason = 'inferred'`` and have no ``run_key``.

Client rows win, but they are checked against the transcript at every refresh (see
:func:`reconcile_client_sessions`): an opening that was never ended by a SessionEnd (the idle timer
closed it, and it only knows when the last upload arrived) ends at its last message instead, and
conversation long after it, with no start reported (the hook did not run), becomes an inferred
session of its own. Inference runs again at every refresh, so the rows follow the transcript.

Which session an event belongs to is a question of time. :data:`CHAT_SESSION_OF_EVENT` is a SQL
fragment for it (no view is needed, so the schema stays as it is)::

    SELECT e.event_id, cs.seq
      FROM event e
      LEFT JOIN LATERAL (<fragment>) cs ON TRUE

The session is the latest one that started at or before the event; an event before the first start,
or without a time, has none.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFAULT_IDLE_MINUTES = 30
_IDLE_MINUTES_VARIABLE = "CCP_SESSION_IDLE_MINUTES"

# Use inside ``LEFT JOIN LATERAL (...) cs ON TRUE``; ``e`` is the event's alias.
CHAT_SESSION_OF_EVENT = """
    SELECT s.chat_session_id, s.seq
      FROM chat_session s
     WHERE s.session_id = e.session_id AND s.started_at <= e.occurred_at
     ORDER BY s.started_at DESC, s.seq DESC
     LIMIT 1
"""


@dataclass(frozen=True)
class InferredSession:
    """One session worked out from time stamps.

    Attributes:
        seq: Its number within the chat (1, 2, ...).
        started_at: The first time stamp of the session.
        ended_at: The last time stamp of the session.
    """

    seq: int
    started_at: datetime
    ended_at: datetime


def idle_minutes_from_environment(env: dict[str, str] | None = None) -> int:
    """Read ``CCP_SESSION_IDLE_MINUTES`` (1 to 1440); anything else gives the default of 30."""
    value = (env if env is not None else os.environ).get(_IDLE_MINUTES_VARIABLE, "")
    try:
        minutes = int(value)
    except ValueError:
        return DEFAULT_IDLE_MINUTES
    return minutes if 1 <= minutes <= 1440 else DEFAULT_IDLE_MINUTES


def infer_sessions(
    times: Sequence[datetime], idle_minutes: int = DEFAULT_IDLE_MINUTES
) -> list[InferredSession]:
    """Split the time stamps of a chat's main transcript into sessions.

    Args:
        times: The time stamps in the order of the transcript's lines (lines without a time are
            left out by the caller). A time that goes backwards does not start a session.
        idle_minutes: A longer gap between two lines starts a new session.

    Returns:
        The sessions in order; empty when there are no time stamps.
    """
    limit = timedelta(minutes=idle_minutes)
    sessions: list[InferredSession] = []
    start: datetime | None = None
    last: datetime | None = None
    for moment in times:
        if start is None or last is None:
            start = last = moment
        elif moment - last > limit:
            sessions.append(InferredSession(len(sessions) + 1, start, last))
            start = last = moment
        else:
            last = max(last, moment)
    if start is not None and last is not None:
        sessions.append(InferredSession(len(sessions) + 1, start, last))
    return sessions


@dataclass(frozen=True)
class ClientSession:
    """A session the capture client reported (it has a run key)."""

    chat_session_id: int
    started_at: datetime
    ended_at: datetime | None
    end_reason: str | None


# Ends that only guess when the session stopped; a real SessionEnd reason is never replaced.
_GUESSED_ENDS = (None, "idle_timeout", "inferred")


def reconcile_client_sessions(
    clients: Sequence[ClientSession], times: Sequence[datetime], idle_minutes: int
) -> tuple[dict[int, datetime], list[tuple[datetime, datetime]]]:
    """Check reported sessions against the conversation's time stamps.

    Args:
        clients: The sessions the capture client reported (any order).
        times: The time stamps of the conversation, in transcript order.
        idle_minutes: A longer gap starts a new session.

    Returns:
        ``(ends, tails)``: the corrected end of each reported session whose end was only guessed
        (by id), and the ``(start, end)`` of conversation that follows such a session after a gap
        and belongs to no reported session. A session without messages ends where it started.
    """
    limit = timedelta(minutes=idle_minutes)
    ordered = sorted(clients, key=lambda c: (c.started_at, c.chat_session_id))
    ends: dict[int, datetime] = {}
    tails: list[tuple[datetime, datetime]] = []
    for index, client in enumerate(ordered):
        if client.end_reason not in _GUESSED_ENDS:
            continue
        following = ordered[index + 1].started_at if index + 1 < len(ordered) else None
        span = [t for t in times if t >= client.started_at and (following is None or t < following)]
        parts = infer_sessions(span, idle_minutes)
        if parts and parts[0].started_at - client.started_at <= limit:
            ends[client.chat_session_id] = max(client.started_at, parts[0].ended_at)
            parts = parts[1:]
        else:
            ends[client.chat_session_id] = client.started_at
        tails += [(part.started_at, part.ended_at) for part in parts]
    return ends, tails


# The time stamps of the conversation itself: user, assistant and system lines of the main
# transcript, in the order of the file. Bookkeeping lines (queue operations, API request records,
# snapshots) are left out: they can be written long after the conversation moved on.
_CONVERSATION_TIMES = """
    SELECT occurred_at FROM event
     WHERE session_id = %s AND split_part(raw_object_key, '/', 3) = 'main'
       AND occurred_at IS NOT NULL AND payload ->> 'type' IN ('user', 'assistant', 'system')
     ORDER BY raw_object_key, raw_line_number
"""

_HAS_CLIENT_SESSIONS = (
    "SELECT 1 FROM chat_session WHERE session_id = %s AND run_key IS NOT NULL LIMIT 1"
)
_DELETE_INFERRED = "DELETE FROM chat_session WHERE session_id = %s AND run_key IS NULL"
_DELETE_SURPLUS_INFERRED = (
    "DELETE FROM chat_session WHERE session_id = %s AND run_key IS NULL AND seq > %s"
)

# Row by row so that a row keeps its id when the transcript grows (corrections point at it).
_UPSERT_INFERRED = """
    INSERT INTO chat_session (session_id, seq, start_source, started_at, ended_at, end_reason)
    VALUES (%s, %s, 'inferred', %s, %s, 'inferred')
    ON CONFLICT (session_id, seq) DO UPDATE SET
        started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at
     WHERE chat_session.run_key IS NULL
"""

# The capture API takes the chat's row lock before it numbers a new session; taking it too means
# that an inferred row and a client row never get the same number.
_LOCK_CHAT = "SELECT 1 FROM session WHERE session_id = %s FOR UPDATE"


_CLIENT_SESSIONS = """
    SELECT chat_session_id, started_at, ended_at, end_reason FROM chat_session
     WHERE session_id = %s AND run_key IS NOT NULL AND started_at IS NOT NULL
"""
_INFERRED_ROWS = """
    SELECT chat_session_id FROM chat_session WHERE session_id = %s AND run_key IS NULL
     ORDER BY started_at NULLS LAST, seq
"""
_SET_END = (
    "UPDATE chat_session SET ended_at = %s WHERE chat_session_id = %s "
    "AND ended_at IS DISTINCT FROM %s"
)
_SET_TAIL = "UPDATE chat_session SET started_at = %s, ended_at = %s WHERE chat_session_id = %s"
_INSERT_TAIL = """
    INSERT INTO chat_session (session_id, seq, start_source, started_at, ended_at, end_reason)
    SELECT %s, COALESCE(max(seq), 0) + 1, 'inferred', %s, %s, 'inferred'
      FROM chat_session WHERE session_id = %s
"""
_DELETE_ROW = "DELETE FROM chat_session WHERE chat_session_id = %s"


def _refresh_with_client_sessions(
    cursor: Any, session_id: str, idle_minutes: int, times: list[datetime]
) -> None:
    """Correct reported sessions and keep inferred rows only for conversation they do not cover."""
    cursor.execute(_CLIENT_SESSIONS, (session_id,))
    clients = [ClientSession(*row) for row in cursor.fetchall()]
    ends, tails = reconcile_client_sessions(clients, times, idle_minutes)
    for chat_session_id, ended_at in ends.items():
        cursor.execute(_SET_END, (ended_at, chat_session_id, ended_at))
    cursor.execute(_INFERRED_ROWS, (session_id,))
    existing = [row[0] for row in cursor.fetchall()]
    for row_id, (started, ended) in zip(existing, tails, strict=False):
        cursor.execute(_SET_TAIL, (started, ended, row_id))
    for started, ended in tails[len(existing) :]:
        cursor.execute(_INSERT_TAIL, (session_id, started, ended, session_id))
    for row_id in existing[len(tails) :]:
        cursor.execute(_DELETE_ROW, (row_id,))


def refresh_chat_sessions(cursor: Any, session_id: str, idle_minutes: int) -> None:
    """Bring the chat's inferred sessions up to date (see the module description).

    Part of the indexer's refresh, in its transaction. A chat that has sessions reported by the
    capture client keeps them, with guessed ends corrected, and gets inferred rows only for
    conversation after a gap that no reported session covers.

    Args:
        cursor: A cursor of the refresh's connection.
        session_id: The chat.
        idle_minutes: The gap that starts a new session.
    """
    cursor.execute(_LOCK_CHAT, (session_id,))
    cursor.execute(_HAS_CLIENT_SESSIONS, (session_id,))
    has_client = cursor.fetchone() is not None
    cursor.execute(_CONVERSATION_TIMES, (session_id,))
    times = [row[0] for row in cursor.fetchall()]
    if has_client:
        _refresh_with_client_sessions(cursor, session_id, idle_minutes, times)
        return
    sessions = infer_sessions(times, idle_minutes)
    for item in sessions:
        cursor.execute(_UPSERT_INFERRED, (session_id, item.seq, item.started_at, item.ended_at))
    cursor.execute(_DELETE_SURPLUS_INFERRED, (session_id, len(sessions)))
