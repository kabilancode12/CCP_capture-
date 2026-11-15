"""The sessions of a chat: when it was opened, and for how long (table ``chat_session``).

The capture client reports each opening itself (SessionStart / SessionEnd hooks); those rows carry a
``run_key`` and are never touched by the indexer. A chat captured before that, or whose hooks never
ran, has none, so the indexer **infers** its sessions from the main transcript's time stamps: a new
session starts at the first line, and again after any gap longer than ``CCP_SESSION_IDLE_MINUTES``
(default 30, the same limit that closes an idle chat) between two lines. Inferred rows are marked
``start_source = 'inferred'``, ``end_reason = 'inferred'`` and have no ``run_key``.

Client rows win: as soon as a chat has one, its inferred rows are removed. Inference runs again at
every refresh, so the rows follow the transcript.

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


def refresh_chat_sessions(cursor: Any, session_id: str, idle_minutes: int) -> None:
    """Bring the chat's inferred sessions up to date (see the module description).

    Part of the indexer's refresh, in its transaction. A chat that has sessions reported by the
    capture client has no inferred ones; any it had before are removed.

    Args:
        cursor: A cursor of the refresh's connection.
        session_id: The chat.
        idle_minutes: The gap that starts a new session.
    """
    cursor.execute(_LOCK_CHAT, (session_id,))
    cursor.execute(_HAS_CLIENT_SESSIONS, (session_id,))
    if cursor.fetchone() is not None:
        cursor.execute(_DELETE_INFERRED, (session_id,))
        return
    cursor.execute(_CONVERSATION_TIMES, (session_id,))
    sessions = infer_sessions([row[0] for row in cursor.fetchall()], idle_minutes)
    for item in sessions:
        cursor.execute(_UPSERT_INFERRED, (session_id, item.seq, item.started_at, item.ended_at))
    cursor.execute(_DELETE_SURPLUS_INFERRED, (session_id, len(sessions)))
