"""Rebuilding the index of a session, and proving that the rebuild gives the same result.

The index is derived data: everything in it comes from the transcripts in MinIO. That is only worth
something if it is *true*, so this module can delete a session's index, build it again from the
stored transcripts, and compare the two. The comparison uses a **fingerprint** per table: a hash of
the table's rows for one session, written without the database's own ids (an event is identified by
the object and line it came from, never by ``event_id``), so two builds of the same transcript have
the same fingerprints.

What is rebuilt: events, thinking, model usage and cost, compactions, sub-agent runs, file changes,
test runs, and the session's start time and branch. What is never touched: the session row itself,
the environment record and the redaction log (they come from the capture client, not from the
transcript).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pg8000.dbapi

from ccp.db.connection import connect
from ccp.index.repository import LOCK_SESSION, IndexRepositoryError
from ccp.index.worker import ChunkStore, JobRepository, index_session
from ccp.settings import DatabaseSettings


def _row(*expressions: str) -> str:
    """SQL for one text line made of the given expressions; NULL and an empty string differ."""
    return "concat_ws('|', " + ", ".join(f"quote_nullable({e})" for e in expressions) + ")"


def _event_key(alias: str) -> str:
    """SQL for the natural key of an event: the object and line it came from."""
    return f"({alias}.raw_object_key || '#' || {alias}.raw_line_number)"


# One query per table. Each returns the table's rows for one session (parameter %s) as text lines,
# in an order that does not depend on the database's own ids.
_FINGERPRINTS: dict[str, str] = {
    "event": (
        "SELECT "  # noqa: S608  (only constants of this file are joined)
        + _row(
            "raw_object_key",
            "raw_line_number",
            "entry_uuid",
            "parent_uuid",
            "event_type",
            "actor",
            "occurred_at",
            "is_abandoned",
            "tool_use_ids",
            "md5(payload::text)",
        )
        + " FROM event WHERE session_id = %s ORDER BY raw_object_key, raw_line_number"
    ),
    "thinking": (
        "SELECT "  # noqa: S608
        + _row(_event_key("e"), "t.block_index", "t.text", "t.is_redacted")
        + " FROM thinking t JOIN event e USING (event_id) WHERE e.session_id = %s"
        " ORDER BY e.raw_object_key, e.raw_line_number, t.block_index"
    ),
    "model_usage": (
        "SELECT "  # noqa: S608
        + _row(
            "m.message_id",
            _event_key("e"),
            "m.model",
            "m.input_tokens",
            "m.output_tokens",
            "m.thinking_tokens",
            "m.cache_read_tokens",
            "m.cache_creation_tokens",
        )
        + " FROM model_usage m JOIN event e USING (event_id) WHERE m.session_id = %s"
        " ORDER BY m.message_id"
    ),
    "session_cost": (
        "SELECT "  # noqa: S608
        + _row(
            "total_cost_usd",
            "has_unknown_model_cost",
            "lines_added",
            "lines_removed",
            "api_duration_ms",
            "tool_duration_ms",
        )
        + " FROM session_cost WHERE session_id = %s"
    ),
    "session_model_cost": (
        "SELECT "  # noqa: S608
        + _row(
            "model",
            "input_tokens",
            "output_tokens",
            "thinking_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "web_search_requests",
            "cost_usd",
        )
        + " FROM session_model_cost WHERE session_id = %s ORDER BY model"
    ),
    "compaction": (
        "SELECT "  # noqa: S608
        + _row(_event_key("e"), "c.trigger", "c.pre_tokens", "c.post_tokens", "c.summary")
        + " FROM compaction c JOIN event e USING (event_id) WHERE c.session_id = %s"
        " ORDER BY e.raw_object_key, e.raw_line_number"
    ),
    "agent_run": (
        "SELECT "  # noqa: S608
        + _row(
            "a.agent_id",
            "a.agent_type",
            "a.description",
            "a.model",
            "a.spawn_depth",
            "a.tool_use_id",
            _event_key("l"),
            "a.instructions",
            "a.result",
            "a.started_at",
            "a.ended_at",
            "a.event_count",
        )
        + " FROM agent_run a LEFT JOIN event l ON l.event_id = a.launch_event_id"
        " WHERE a.session_id = %s ORDER BY a.agent_id"
    ),
    "file_change": (
        "SELECT "  # noqa: S608
        + _row(
            _event_key("e"),
            "f.tool_use_id",
            _event_key("c"),
            "f.tool",
            _event_key("p"),
            "f.path",
            "f.absolute_path",
            "f.change_type",
            "f.actor_class",
            "f.user_modified",
            "f.lines_added",
            "f.lines_removed",
            "f.hunks::text",
        )
        + " FROM file_change f JOIN event e ON e.event_id = f.event_id"
        " LEFT JOIN event c ON c.event_id = f.call_event_id"
        " LEFT JOIN event p ON p.event_id = f.prompt_event_id"
        " WHERE f.session_id = %s ORDER BY e.raw_object_key, e.raw_line_number"
    ),
    "test_run": (
        "SELECT "  # noqa: S608
        + _row(
            _event_key("c"),
            _event_key("r"),
            "t.tool_use_id",
            "t.command",
            "t.framework",
            "t.passed",
            "t.exit_code",
        )
        + " FROM test_run t JOIN event c ON c.event_id = t.call_event_id"
        " LEFT JOIN event r ON r.event_id = t.result_event_id"
        " WHERE t.session_id = %s ORDER BY c.raw_object_key, c.raw_line_number"
    ),
    "session": (
        "SELECT "  # noqa: S608
        + _row("started_at", "branch")
        + " FROM session WHERE session_id = %s"
    ),
}

_ALL_SESSIONS = "SELECT session_id FROM session ORDER BY session_id"
_SESSION_EXISTS = "SELECT 1 FROM session WHERE session_id = %s"
_QUEUE_JOB = """
    INSERT INTO job (job_type, session_id) VALUES ('index_session', %s)
    ON CONFLICT (session_id) WHERE job_type = 'index_session' AND status = 'queued' DO NOTHING
"""
# Deleting a session's events removes, through the foreign keys, its thinking, model usage,
# compactions, file changes and test runs. The other derived tables are cleared by name. The
# session's lock comes first, so a worker indexing the session at the same time waits (or is
# waited for) instead of mixing its rows with the reset.
_RESET_STATEMENTS = (
    LOCK_SESSION,
    "DELETE FROM agent_run WHERE session_id = %s",
    "DELETE FROM session_cost WHERE session_id = %s",
    "DELETE FROM session_model_cost WHERE session_id = %s",
    "DELETE FROM event WHERE session_id = %s",
    "UPDATE session SET started_at = NULL, branch = NULL WHERE session_id = %s",
)


@dataclass(frozen=True)
class RebuildResult:
    """What a rebuild of one session did."""

    session_id: str
    events: int  # events after the rebuild
    differences: tuple[str, ...]  # tables whose fingerprint changed (empty: identical)


class RebuildRepository:
    """Database access for rebuilding: listing sessions, clearing the index, fingerprints."""

    def __init__(self, settings: DatabaseSettings) -> None:
        """Remember the connection settings; nothing is connected yet."""
        self._settings = settings

    def session_ids(self) -> list[str]:
        """Every session known to the database."""
        return [str(row[0]) for row in self._query(_ALL_SESSIONS, ())]

    def exists(self, session_id: str) -> bool:
        """Whether the session is known to the database."""
        return bool(self._query(_SESSION_EXISTS, (session_id,)))

    def queue_job(self, session_id: str) -> None:
        """Queue an index job for the session (nothing happens if one is already waiting)."""
        self._execute((_QUEUE_JOB,), session_id)

    def reset(self, session_id: str) -> None:
        """Delete the session's index, in one transaction. The transcripts are not touched."""
        self._execute(_RESET_STATEMENTS, session_id)

    def fingerprints(self, session_id: str) -> dict[str, str]:
        """A hash per table of the session's rows, free of the database's own ids."""
        result = {}
        for table, sql in _FINGERPRINTS.items():
            digest = hashlib.sha256()
            for row in self._query(sql, (session_id,)):
                digest.update(str(row[0]).encode("utf-8"))
                digest.update(b"\n")
            result[table] = digest.hexdigest()
        return result

    def event_count(self, session_id: str) -> int:
        """How many events the session has."""
        rows = self._query("SELECT count(*) FROM event WHERE session_id = %s", (session_id,))
        return int(str(rows[0][0]))

    def _query(self, sql: str, parameters: tuple[object, ...]) -> list[list[object]]:
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(sql, parameters)
            return [list(row) for row in cursor.fetchall()]
        except (pg8000.dbapi.Error, OSError) as error:
            raise IndexRepositoryError(f"a query failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def _execute(self, statements: tuple[str, ...], session_id: str) -> None:
        connection = self._open()
        try:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement, (session_id,))
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"a statement failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def _open(self) -> pg8000.dbapi.Connection:
        try:
            return connect(self._settings)
        except (pg8000.dbapi.Error, OSError) as error:
            raise IndexRepositoryError(
                f"database connection failed: {type(error).__name__}"
            ) from error


def rebuild_session(
    store: ChunkStore,
    index: JobRepository,
    rebuild: RebuildRepository,
    session_id: str,
    *,
    check: bool,
) -> RebuildResult | None:
    """Delete a session's index and build it again from the stored transcripts.

    Without ``check`` the index is only deleted and a job is queued, so that a running
    ``ccp-index run`` builds it again. With ``check`` it is built again right away, and compared
    with what it was before.

    Args:
        store: Where the transcript chunks are.
        index: The indexer's database access.
        rebuild: The rebuild's database access.
        session_id: The session to rebuild.
        check: Rebuild now and compare, instead of queueing.

    Returns:
        The result for a ``check`` run; None when the work was only queued.
    """
    before = rebuild.fingerprints(session_id) if check else {}
    # The job goes in first: if this process stops half way, a worker finishes the rebuild.
    rebuild.queue_job(session_id)
    rebuild.reset(session_id)
    if not check:
        return None
    index_session(store, index, session_id)
    after = rebuild.fingerprints(session_id)
    differences = tuple(table for table in after if after[table] != before[table])
    return RebuildResult(session_id, rebuild.event_count(session_id), differences)
