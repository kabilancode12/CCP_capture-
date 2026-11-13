"""The indexer's use of the database: the job queue and the event rows.

Every method opens a short-lived connection, does its work in one transaction and closes the
connection, like :mod:`ccp.ingest.repository`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

import pg8000.dbapi

from ccp.db.connection import connect
from ccp.index.parser import ParsedEntry
from ccp.index.tree import TreeNode, abandoned_event_ids
from ccp.settings import DatabaseSettings

# A job that failed is tried again this many times in all, then left as 'failed'.
MAX_ATTEMPTS = 3

# Take the oldest queued index job. FOR UPDATE SKIP LOCKED lets several workers run at once:
# each takes a different job, and none waits for another.
_CLAIM_JOB = """
    UPDATE job SET status = 'running', attempts = attempts + 1
     WHERE job_id = (
        SELECT job_id FROM job
         WHERE job_type = 'index_session' AND status = 'queued'
         ORDER BY created_at, job_id
           FOR UPDATE SKIP LOCKED
         LIMIT 1)
    RETURNING job_id, session_id, attempts
"""

_FINISH_JOB = "UPDATE job SET status = 'done', finished_at = now() WHERE job_id = %s"

# Put a failed job back in the queue, unless it has used its attempts or the session already has
# a queued job (a new chunk arrived meanwhile; that job will index everything anyway).
_RETRY_JOB = """
    UPDATE job SET status = 'queued'
     WHERE job_id = %s AND attempts < %s
       AND NOT EXISTS (SELECT 1 FROM job other
                        WHERE other.job_type = 'index_session' AND other.status = 'queued'
                          AND other.session_id = job.session_id)
"""
_FAIL_JOB = "UPDATE job SET status = 'failed', finished_at = now() WHERE job_id = %s"

# Chunks whose events are already stored. A chunk is stored in one transaction, so it is either
# fully indexed or not at all.
_INDEXED_KEYS = "SELECT DISTINCT raw_object_key FROM event WHERE session_id = %s"

# Insert one event. If it exists already (same object, same line) nothing changes, but the id is
# still returned, so re-indexing is safe and the thinking rows can be attached.
_INSERT_EVENT = """
    INSERT INTO event (session_id, entry_uuid, parent_uuid, event_type, actor, occurred_at,
                       payload, raw_object_key, raw_line_number)
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
    ON CONFLICT (raw_object_key, raw_line_number)
    DO UPDATE SET session_id = EXCLUDED.session_id
    RETURNING event_id
"""

_INSERT_THINKING = """
    INSERT INTO thinking (event_id, block_index, text, is_redacted) VALUES (%s, %s, %s, %s)
    ON CONFLICT (event_id, block_index) DO NOTHING
"""


# The events the tree rule needs. A prompt counts as the developer's only if a human wrote it and
# Claude Code did not inject it (isMeta).
_TREE_NODES = """
    SELECT event_id, entry_uuid, parent_uuid, payload->>'logicalParentUuid',
           event_type = 'prompt' AND actor = 'human'
               AND COALESCE(payload->>'isMeta', 'false') <> 'true',
           raw_object_key, raw_line_number
      FROM event WHERE session_id = %s
"""

# Set the flag on exactly the abandoned events; rows that already have the right value are not
# touched, so repeating this changes nothing.
_MARK_ABANDONED = """
    UPDATE event SET is_abandoned = (event_id = ANY(%s::bigint[]))
     WHERE session_id = %s AND is_abandoned IS DISTINCT FROM (event_id = ANY(%s::bigint[]))
"""

# One compaction row per boundary entry, with the summary that follows it (the user entry whose
# parent is the boundary). The summary may be indexed in a later chunk than the boundary, so an
# existing row is brought up to date.
_REFRESH_COMPACTIONS = """
    INSERT INTO compaction (session_id, event_id, trigger, pre_tokens, post_tokens, summary)
    SELECT b.session_id, b.event_id,
           b.payload #>> '{compactMetadata,trigger}',
           CASE WHEN jsonb_typeof(b.payload #> '{compactMetadata,preTokens}') = 'number'
                THEN (b.payload #>> '{compactMetadata,preTokens}')::numeric::bigint END,
           CASE WHEN jsonb_typeof(b.payload #> '{compactMetadata,postTokens}') = 'number'
                THEN (b.payload #>> '{compactMetadata,postTokens}')::numeric::bigint END,
           (SELECT s.payload #>> '{message,content}' FROM event s
             WHERE s.session_id = b.session_id AND s.parent_uuid = b.entry_uuid
               AND s.event_type = 'compaction_summary'
             ORDER BY s.event_id LIMIT 1)
      FROM event b
     WHERE b.session_id = %s AND b.event_type = 'compact_boundary'
    ON CONFLICT (event_id) DO UPDATE SET
        trigger = EXCLUDED.trigger, pre_tokens = EXCLUDED.pre_tokens,
        post_tokens = EXCLUDED.post_tokens, summary = EXCLUDED.summary
"""

# Facts about the session that the transcript knows: when it started, and on which Git branch.
# Only the main transcript counts (sub-agent streams are the third part of the key), and the
# branch is the first one named. A known value is replaced; if the transcript says nothing, a
# value stored earlier stays.
_REFRESH_SESSION_FACTS = """
    UPDATE session SET
        started_at = COALESCE(f.first_at, session.started_at),
        branch = COALESCE(f.first_branch, session.branch)
      FROM (SELECT min(occurred_at) AS first_at,
                   (array_agg(payload->>'gitBranch' ORDER BY raw_object_key, raw_line_number)
                       FILTER (WHERE payload->>'gitBranch' IS NOT NULL
                                 AND payload->>'gitBranch' <> ''))[1] AS first_branch
              FROM event
             WHERE session_id = %s AND split_part(raw_object_key, '/', 3) = 'main') AS f
     WHERE session.session_id = %s
"""


class IndexRepositoryError(RuntimeError):
    """Raised when the database cannot be reached or a statement fails."""


@dataclass(frozen=True)
class IndexJob:
    """A claimed job: index this session."""

    job_id: int
    session_id: str
    attempts: int


class IndexRepository:
    """Database access for the indexer, backed by PostgreSQL."""

    def __init__(self, settings: DatabaseSettings) -> None:
        """Remember the connection settings; nothing is connected yet."""
        self._settings = settings

    def claim_job(self) -> IndexJob | None:
        """Take the oldest queued index job and mark it running; None if the queue is empty."""
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_CLAIM_JOB)
            row = cursor.fetchone()
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"claiming a job failed: {type(error).__name__}") from error
        finally:
            connection.close()
        return IndexJob(int(row[0]), str(row[1]), int(row[2])) if row else None

    def finish_job(self, job_id: int) -> None:
        """Mark a job done."""
        self._run(_FINISH_JOB, (job_id,), "finishing a job")

    def fail_job(self, job_id: int) -> bool:
        """Record that a job failed.

        Returns:
            True if the job was queued again, False if it is now left as ``failed``.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_RETRY_JOB, (job_id, MAX_ATTEMPTS))
            requeued = bool(cursor.rowcount == 1)
            if not requeued:
                cursor.execute(_FAIL_JOB, (job_id,))
            connection.commit()
            return requeued
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"failing a job failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def indexed_keys(self, session_id: str) -> set[str]:
        """The transcript chunks of this session that already have their events stored."""
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_INDEXED_KEYS, (session_id,))
            return {str(row[0]) for row in cursor.fetchall()}
        except (pg8000.dbapi.Error, OSError) as error:
            raise IndexRepositoryError(
                f"reading indexed chunks failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()

    def save_chunk(
        self, session_id: str, object_key: str, entries: Sequence[tuple[int, ParsedEntry]]
    ) -> None:
        """Store the events (and thinking blocks) of one chunk, in one transaction.

        Args:
            session_id: The session the chunk belongs to.
            object_key: The storage key of the chunk.
            entries: ``(line number, entry)`` pairs; line numbers start at 1 within the chunk.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            for line_number, entry in entries:
                cursor.execute(
                    _INSERT_EVENT,
                    (
                        session_id,
                        entry.entry_uuid,
                        entry.parent_uuid,
                        entry.event_type,
                        entry.actor,
                        entry.occurred_at,
                        json.dumps(entry.payload, ensure_ascii=False),
                        object_key,
                        line_number,
                    ),
                )
                row = cursor.fetchone()
                if row is None:  # cannot happen: RETURNING always yields the row
                    raise IndexRepositoryError("an event insert returned no id")
                for block in entry.thinking:
                    cursor.execute(
                        _INSERT_THINKING,
                        (int(row[0]), block.block_index, block.text, block.is_redacted),
                    )
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"saving events failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def refresh_session(self, session_id: str) -> None:
        """Recompute everything that is derived from a session's events, in one transaction.

        That is: which events are on abandoned branches, the compaction rows, and the session's
        start time and branch. It reads all of the session's events, so the result is the same
        however many chunks were indexed in which order, and repeating it changes nothing.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_TREE_NODES, (session_id,))
            nodes = [
                TreeNode(
                    event_id=int(row[0]),
                    entry_uuid=row[1],
                    parent_uuid=row[2],
                    logical_parent_uuid=row[3],
                    is_user_prompt=bool(row[4]),
                    position=(str(row[5]), int(row[6])),
                )
                for row in cursor.fetchall()
            ]
            abandoned = sorted(abandoned_event_ids(nodes))
            cursor.execute(_MARK_ABANDONED, (abandoned, session_id, abandoned))
            cursor.execute(_REFRESH_COMPACTIONS, (session_id,))
            cursor.execute(_REFRESH_SESSION_FACTS, (session_id, session_id))
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(
                f"refreshing a session failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()

    def _run(self, sql: str, parameters: tuple[object, ...], what: str) -> None:
        """Run one statement in its own transaction."""
        connection = self._open()
        try:
            connection.cursor().execute(sql, parameters)
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"{what} failed: {type(error).__name__}") from error
        finally:
            connection.close()

    def _open(self) -> pg8000.dbapi.Connection:
        """Open a connection, turning connection failures into :class:`IndexRepositoryError`."""
        try:
            return connect(self._settings)
        except (pg8000.dbapi.Error, OSError) as error:
            raise IndexRepositoryError(
                f"database connection failed: {type(error).__name__}"
            ) from error
