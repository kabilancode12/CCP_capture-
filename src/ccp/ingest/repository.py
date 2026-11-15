"""The ingestion API's use of the database.

The API depends on the small :class:`SessionRepository` interface, not on PostgreSQL directly, so
its behaviour can be tested with a simple fake, and the real database code is tested separately.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

import pg8000.dbapi

from ccp.capture.gitinfo import strip_credentials
from ccp.db.connection import connect
from ccp.db.hierarchy import link_chat
from ccp.environment import FIELD_NAMES, EnvironmentRecord
from ccp.lifecycle import START, LifecycleEvent
from ccp.redaction.records import RedactionRecord
from ccp.settings import DatabaseSettings

# Create the session if it is new; otherwise note the activity and (re)open it, so a late chunk
# of a session that was marked closed reopens it.
_UPSERT_SESSION = """
    INSERT INTO session (session_id) VALUES (%s)
    ON CONFLICT (session_id) DO UPDATE SET last_activity_at = now(), status = 'open'
"""

# Queue an index job, unless one is already waiting for this session (see migration 002).
_QUEUE_INDEX_JOB = """
    INSERT INTO job (job_type, session_id) VALUES ('index_session', %s)
    ON CONFLICT (session_id) WHERE job_type = 'index_session' AND status = 'queued' DO NOTHING
"""


# Record redaction hits; repeating the same report only sets the same counts again.
_UPSERT_REDACTION = """
    INSERT INTO redaction_log (session_id, rule_id, location, occurrences) VALUES (%s, %s, %s, %s)
    ON CONFLICT (session_id, rule_id, location) DO UPDATE SET occurrences = EXCLUDED.occurrences
"""

# Insert or update the environment record. Every column is listed, with NULL for unknown ones; on
# conflict a known value replaces the stored one and an unknown (NULL) value leaves it unchanged,
# so partial updates merge. The column names come from the constant FIELD_NAMES, never from input.
_UPSERT_ENVIRONMENT = (
    "INSERT INTO session_env (session_id, "  # noqa: S608
    + ", ".join(FIELD_NAMES)
    + ") VALUES (%s"
    + ", %s" * len(FIELD_NAMES)
    + ") ON CONFLICT (session_id) DO UPDATE SET "
    + ", ".join(f"{name} = COALESCE(EXCLUDED.{name}, session_env.{name})" for name in FIELD_NAMES)
)

# Make sure the session exists (session_env refers to it) without touching its activity.
# A chat first seen through its environment record or a start/end has no message yet: it starts
# as empty, and the indexer clears the flag once its transcript holds a prompt or a reply.
_ENSURE_SESSION = (
    "INSERT INTO session (session_id, is_empty) VALUES (%s, TRUE) "
    "ON CONFLICT (session_id) DO NOTHING"
)

# Close every open session that has been quiet for %s minutes, and queue one final index job for
# each so nothing indexed earlier is stale. A session of the chat (chat_session) that never received
# its end is ended too, at the last activity. One statement, so it is atomic. A session that
# receives a chunk at the same moment is not closed: the row is re-checked under lock, and the
# upload's `last_activity_at = now()` makes the condition false.
_CLOSE_IDLE_SESSIONS = """
    WITH closed AS (
        UPDATE session
           SET status = 'closed',
               ended_at = COALESCE(ended_at, last_activity_at),
               end_reason = COALESCE(end_reason, 'idle_timeout')
         WHERE status = 'open'
           AND last_activity_at < now() - make_interval(mins => %s)
        RETURNING session_id, last_activity_at
    ), queued AS (
        INSERT INTO job (job_type, session_id)
        SELECT 'index_session', session_id FROM closed
        ON CONFLICT (session_id) WHERE job_type = 'index_session' AND status = 'queued' DO NOTHING
    ), runs AS (
        UPDATE chat_session r
           SET ended_at = GREATEST(c.last_activity_at, r.started_at), end_reason = 'idle_timeout'
          FROM closed c
         WHERE r.session_id = c.session_id AND r.ended_at IS NULL
    )
    SELECT session_id FROM closed ORDER BY session_id
"""

# --- lifecycle events (a session of the chat started or ended; see ccp.lifecycle) ---

# A start counts as activity: it (re)opens the chat, so the idle closer can end the session later
# even if no message is ever written. A reopened chat has not ended any more, so its end is cleared
# (the next end or the idle closer sets it again). A start that arrives late (it waited on the
# developer's machine) and is older than the chat's last activity or its recorded end reopens
# nothing. The start's time is the client's clock, so a large clock difference can still reopen a
# chat once; the idle closer then closes it again. An end only makes sure the chat exists.
_ENSURE_SESSION_FOR_START = """
    INSERT INTO session (session_id, is_empty) VALUES (%s, TRUE)
    ON CONFLICT (session_id) DO UPDATE SET
        last_activity_at = now(), status = 'open', ended_at = NULL, end_reason = NULL
     WHERE session.last_activity_at < %s AND (session.ended_at IS NULL OR session.ended_at < %s)
"""

# Lock the chat's row, so two events of one chat get their sequence numbers one after the other.
_LOCK_CHAT = "SELECT 1 FROM session WHERE session_id = %s FOR UPDATE"

_FIND_RUN = "SELECT session_id FROM chat_session WHERE run_key = %s"

_INSERT_RUN = """
    INSERT INTO chat_session (session_id, seq, run_key)
    SELECT %s, COALESCE(max(seq), 0) + 1, %s FROM chat_session WHERE session_id = %s
"""

_RECORD_START = """
    UPDATE chat_session SET start_source = %s, started_at = %s, start_commit = %s
     WHERE run_key = %s
"""

# A real end replaces an end that was only inferred or given by the idle timeout.
_RECORD_END = """
    UPDATE chat_session SET ended_at = %s, end_reason = %s, end_commit = %s
     WHERE run_key = %s
"""

# A new start means that an earlier session of the chat that is still open has ended (Claude Code
# runs no SessionEnd before /compact, and none at all if it was killed).
_END_EARLIER_RUNS = """
    UPDATE chat_session SET ended_at = %s, end_reason = 'inferred'
     WHERE session_id = %s AND run_key IS DISTINCT FROM %s AND ended_at IS NULL
       AND (started_at IS NULL OR started_at <= %s)
"""

_CHAT_FACTS_AT_START = """
    UPDATE session SET
        start_source = COALESCE(start_source, %s),
        start_commit = COALESCE(start_commit, %s),
        started_at = COALESCE(started_at, %s)
     WHERE session_id = %s
"""

_CHAT_FACTS_AT_END = """
    UPDATE session SET
        end_reason = %s,
        ended_at = GREATEST(ended_at, %s)
     WHERE session_id = %s AND (ended_at IS NULL OR ended_at <= %s)
"""

_RUN_SEQ = "SELECT seq FROM chat_session WHERE run_key = %s"


class RepositoryError(RuntimeError):
    """Raised when the database cannot be reached or a statement fails."""


class RunKeyConflictError(RepositoryError):
    """Raised when a lifecycle event's run key already belongs to another chat."""


class SessionRepository(Protocol):
    """What the ingestion API needs from the database."""

    def record_chunk(self, session_id: str) -> None:
        """Record that a chunk of this session arrived.

        Ensures the session exists and is open, and that an index job is queued. Safe to
        repeat: the result is the same each time.
        """

    def record_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        """Store the client's redaction records. Safe to repeat: same result each time."""

    def record_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        """Store or merge the session's environment record. Safe to repeat."""

    def record_lifecycle(self, session_id: str, event: LifecycleEvent) -> int:
        """Record a session start or end of the chat; return the session's number in the chat.

        Safe to repeat, and the start and end of one session may arrive in either order.

        Raises:
            RunKeyConflictError: If the run key belongs to another chat.
        """

    def ping(self) -> None:
        """Raise :class:`RepositoryError` if the database cannot be used."""


class PostgresRepository:
    """:class:`SessionRepository` backed by PostgreSQL. Opens a short-lived connection per call."""

    def __init__(self, settings: DatabaseSettings) -> None:
        """Remember the connection settings; nothing is connected yet."""
        self._settings = settings

    def record_chunk(self, session_id: str) -> None:
        """Upsert the session and queue an index job, in one transaction."""
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_UPSERT_SESSION, (session_id,))
            cursor.execute(_QUEUE_INDEX_JOB, (session_id,))
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise RepositoryError(f"database write failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def record_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        """Store redaction records for a session, in one transaction.

        A record that already exists (same rule and location) only has its count set again, so
        a retried report does not create duplicates.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            for record in records:
                cursor.execute(
                    _UPSERT_REDACTION,
                    (session_id, record.rule_id, record.location, record.occurrences),
                )
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise RepositoryError(f"saving redactions failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def record_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        """Store the environment record, merging it into any record already stored.

        Fields that are known in ``record`` replace the stored values; fields that are unknown
        leave them unchanged. The session row is created if needed, and the chat is linked to its
        Claude account and project (:func:`ccp.db.hierarchy.link_chat`). One transaction.

        A Git remote is stored without credentials, even if a client sent them.
        """
        if record.git_remote:
            record = replace(record, git_remote=strip_credentials(record.git_remote) or None)
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_ENSURE_SESSION, (session_id,))
            values = record.to_payload()
            cursor.execute(
                _UPSERT_ENVIRONMENT, (session_id, *(values.get(name) for name in FIELD_NAMES))
            )
            link_chat(cursor, session_id)
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise RepositoryError(f"saving environment failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def record_lifecycle(self, session_id: str, event: LifecycleEvent) -> int:
        """Record a session start or end in ``chat_session``, in one transaction.

        The row is found by its run key, or added with the next number of the chat. A start sets
        how and when the session began and ends any earlier session of the chat still open; an end
        sets when and why it ended. The chat row keeps its first start source and start commit and
        its latest end.

        Returns:
            The session's number within the chat (1, 2, ...).

        Raises:
            RunKeyConflictError: If the run key belongs to another chat.
            RepositoryError: If the database cannot be used.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            if event.kind == START:
                cursor.execute(_ENSURE_SESSION_FOR_START, (session_id, event.at, event.at))
            else:
                cursor.execute(_ENSURE_SESSION, (session_id,))
            cursor.execute(_LOCK_CHAT, (session_id,))
            cursor.execute(_FIND_RUN, (event.run_key,))
            found = cursor.fetchone()
            if found is None:
                cursor.execute(_INSERT_RUN, (session_id, event.run_key, session_id))
            elif found[0] != session_id:
                connection.rollback()
                raise RunKeyConflictError("the run key belongs to another chat")
            self._apply_lifecycle(cursor, session_id, event)
            cursor.execute(_RUN_SEQ, (event.run_key,))
            seq = int(cursor.fetchone()[0])
            connection.commit()
            return seq
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise RepositoryError(f"saving lifecycle failed: {type(error).__name__}") from error
        finally:
            connection.close()

    @staticmethod
    def _apply_lifecycle(
        cursor: pg8000.dbapi.Cursor, session_id: str, event: LifecycleEvent
    ) -> None:
        """Write a start's or an end's facts to the chat session row and to the chat."""
        if event.kind == START:
            cursor.execute(_RECORD_START, (event.source, event.at, event.commit, event.run_key))
            cursor.execute(_END_EARLIER_RUNS, (event.at, session_id, event.run_key, event.at))
            cursor.execute(_CHAT_FACTS_AT_START, (event.source, event.commit, event.at, session_id))
        else:
            cursor.execute(_RECORD_END, (event.at, event.reason, event.commit, event.run_key))
            cursor.execute(_CHAT_FACTS_AT_END, (event.reason, event.at, session_id, event.at))

    def close_idle_sessions(self, idle_minutes: int) -> list[str]:
        """Close open sessions with no activity for ``idle_minutes``; queue a final index job.

        Args:
            idle_minutes: How long a session must be quiet before it is closed.

        Returns:
            The ids of the sessions closed by this call (empty if none were due).
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_CLOSE_IDLE_SESSIONS, (idle_minutes,))
            closed = [str(row[0]) for row in cursor.fetchall()]
            connection.commit()
            return closed
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise RepositoryError(
                f"closing idle sessions failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()

    def ping(self) -> None:
        """Run a trivial query to prove the database is reachable."""
        connection = self._open()
        try:
            connection.cursor().execute("SELECT 1")
        except (pg8000.dbapi.Error, OSError) as error:
            raise RepositoryError(f"database check failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def _open(self) -> pg8000.dbapi.Connection:
        """Open a connection, turning connection failures into :class:`RepositoryError`."""
        try:
            return connect(self._settings)
        except (pg8000.dbapi.Error, OSError) as error:
            raise RepositoryError(f"database unreachable: {type(error).__name__}") from error
