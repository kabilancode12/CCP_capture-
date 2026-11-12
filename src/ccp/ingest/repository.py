"""The ingestion API's use of the database.

The API depends on the small :class:`SessionRepository` interface, not on PostgreSQL directly, so
its behaviour can be tested with a simple fake, and the real database code is tested separately.
"""

from __future__ import annotations

from typing import Protocol

import pg8000.dbapi

from ccp.db.connection import connect
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


class RepositoryError(RuntimeError):
    """Raised when the database cannot be reached or a statement fails."""


class SessionRepository(Protocol):
    """What the ingestion API needs from the database."""

    def record_chunk(self, session_id: str) -> None:
        """Record that a chunk of this session arrived.

        Ensures the session exists and is open, and that an index job is queued. Safe to
        repeat: the result is the same each time.
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
