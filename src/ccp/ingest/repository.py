"""The ingestion API's use of the database.

The API depends on the small :class:`SessionRepository` interface, not on PostgreSQL directly, so
its behaviour can be tested with a simple fake, and the real database code is tested separately.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import pg8000.dbapi

from ccp.db.connection import connect
from ccp.environment import FIELD_NAMES, EnvironmentRecord
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
_ENSURE_SESSION = "INSERT INTO session (session_id) VALUES (%s) ON CONFLICT (session_id) DO NOTHING"

# Close every open session that has been quiet for %s minutes, and queue one final index job for
# each so nothing indexed earlier is stale. One statement, so it is atomic. A session that receives
# a chunk at the same moment is not closed: the row is re-checked under lock, and the upload's
# `last_activity_at = now()` makes the condition false.
_CLOSE_IDLE_SESSIONS = """
    WITH closed AS (
        UPDATE session
           SET status = 'closed',
               ended_at = COALESCE(ended_at, last_activity_at),
               end_reason = COALESCE(end_reason, 'idle_timeout')
         WHERE status = 'open'
           AND last_activity_at < now() - make_interval(mins => %s)
        RETURNING session_id
    ), queued AS (
        INSERT INTO job (job_type, session_id)
        SELECT 'index_session', session_id FROM closed
        ON CONFLICT (session_id) WHERE job_type = 'index_session' AND status = 'queued' DO NOTHING
    )
    SELECT session_id FROM closed ORDER BY session_id
"""


class RepositoryError(RuntimeError):
    """Raised when the database cannot be reached or a statement fails."""


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
        leave them unchanged. The session row is created if needed. One transaction.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_ENSURE_SESSION, (session_id,))
            values = record.to_payload()
            cursor.execute(
                _UPSERT_ENVIRONMENT, (session_id, *(values.get(name) for name in FIELD_NAMES))
            )
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise RepositoryError(f"saving environment failed: {type(error).__name__}") from error
        finally:
            connection.close()

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
