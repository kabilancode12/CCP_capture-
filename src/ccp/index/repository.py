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
from ccp.index.agents import AgentMeta
from ccp.index.filechanges import extract_file_change
from ccp.index.parser import ParsedEntry
from ccp.index.tree import TreeNode, abandoned_event_ids, nearest_prompts
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
                       payload, raw_object_key, raw_line_number, tool_use_ids)
    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
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


# One row per API message. The same message appears in several entries (one per content block)
# with the same usage, so the first entry of each message is kept.
_REFRESH_MODEL_USAGE = """
    INSERT INTO model_usage (session_id, message_id, event_id, model, input_tokens, output_tokens,
                             thinking_tokens, cache_read_tokens, cache_creation_tokens)
    SELECT DISTINCT ON (COALESCE(payload #>> '{message,id}', entry_uuid))
           session_id, COALESCE(payload #>> '{message,id}', entry_uuid), event_id,
           payload #>> '{message,model}',
           json_bigint(payload #> '{message,usage,input_tokens}'),
           json_bigint(payload #> '{message,usage,output_tokens}'),
           json_bigint(payload #> '{message,usage,output_tokens_details,thinking_tokens}'),
           json_bigint(payload #> '{message,usage,cache_read_input_tokens}'),
           json_bigint(payload #> '{message,usage,cache_creation_input_tokens}')
      FROM event
     WHERE session_id = %s AND event_type IN ('response', 'tool_call', 'thinking')
       AND jsonb_typeof(payload #> '{message,usage}') = 'object'
       AND COALESCE(payload #>> '{message,id}', entry_uuid) IS NOT NULL
     ORDER BY COALESCE(payload #>> '{message,id}', entry_uuid), raw_object_key, raw_line_number
    ON CONFLICT (session_id, message_id) DO NOTHING
"""

# Session cost comes from the latest cost-state entry of the main transcript: its totals cover the
# whole session so far. (The same sub-query is used twice, to keep each statement self-contained.)
_REFRESH_SESSION_COST = """
    INSERT INTO session_cost (session_id, total_cost_usd, has_unknown_model_cost, lines_added,
                              lines_removed, api_duration_ms, tool_duration_ms)
    SELECT %s,
           CASE WHEN jsonb_typeof(l.payload -> 'totalCostUSD') = 'number'
                THEN (l.payload ->> 'totalCostUSD')::numeric END,
           CASE WHEN jsonb_typeof(l.payload -> 'hasUnknownModelCost') = 'boolean'
                THEN (l.payload ->> 'hasUnknownModelCost')::boolean END,
           json_bigint(l.payload -> 'totalLinesAdded'),
           json_bigint(l.payload -> 'totalLinesRemoved'),
           json_bigint(l.payload -> 'totalAPIDuration'),
           json_bigint(l.payload -> 'totalToolDuration')
      FROM (SELECT payload FROM event
             WHERE session_id = %s AND event_type = 'cost-state'
               AND split_part(raw_object_key, '/', 3) = 'main'
             ORDER BY raw_object_key DESC, raw_line_number DESC LIMIT 1) AS l
    ON CONFLICT (session_id) DO UPDATE SET
        total_cost_usd = EXCLUDED.total_cost_usd,
        has_unknown_model_cost = EXCLUDED.has_unknown_model_cost,
        lines_added = EXCLUDED.lines_added, lines_removed = EXCLUDED.lines_removed,
        api_duration_ms = EXCLUDED.api_duration_ms, tool_duration_ms = EXCLUDED.tool_duration_ms
"""

_DELETE_MODEL_COSTS = "DELETE FROM session_model_cost WHERE session_id = %s"

_REFRESH_MODEL_COSTS = """
    INSERT INTO session_model_cost (session_id, model, input_tokens, output_tokens, thinking_tokens,
                                    cache_read_tokens, cache_creation_tokens, web_search_requests,
                                    cost_usd)
    SELECT %s, m.key,
           json_bigint(m.value -> 'inputTokens'),
           json_bigint(m.value -> 'outputTokens'),
           json_bigint(m.value -> 'thinkingTokens'),
           json_bigint(m.value -> 'cacheReadInputTokens'),
           json_bigint(m.value -> 'cacheCreationInputTokens'),
           json_bigint(m.value -> 'webSearchRequests'),
           CASE WHEN jsonb_typeof(m.value -> 'costUSD') = 'number'
                THEN (m.value ->> 'costUSD')::numeric END
      FROM (SELECT payload FROM event
             WHERE session_id = %s AND event_type = 'cost-state'
               AND split_part(raw_object_key, '/', 3) = 'main'
             ORDER BY raw_object_key DESC, raw_line_number DESC LIMIT 1) AS l,
           jsonb_each(CASE WHEN jsonb_typeof(l.payload -> 'modelUsage') = 'object'
                           THEN l.payload -> 'modelUsage' ELSE '{}'::jsonb END) AS m
"""


# A sub-agent's description (from its .meta stream). Uploaded once, so a repeat just sets it again.
_SAVE_AGENT_META = """
    INSERT INTO agent_run (session_id, agent_id, agent_type, description, model, spawn_depth,
                           tool_use_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (session_id, agent_id) DO UPDATE SET
        agent_type = EXCLUDED.agent_type, description = EXCLUDED.description,
        model = EXCLUDED.model, spawn_depth = EXCLUDED.spawn_depth,
        tool_use_id = EXCLUDED.tool_use_id
"""

# Every sub-agent stream seen in the events has a run, even if its description has not arrived.
# (The stream name is "agent-<id>"; the id starts at the 7th character.)
_ENSURE_AGENT_RUNS = """
    INSERT INTO agent_run (session_id, agent_id)
    SELECT DISTINCT session_id, substr(split_part(raw_object_key, '/', 3), 7) FROM event
     WHERE session_id = %s AND split_part(raw_object_key, '/', 3) ~ '^agent-'
    ON CONFLICT (session_id, agent_id) DO NOTHING
"""

# What the events say about each run: the parent's call that launched it (the event holding a
# tool_use with the description's toolUseId; it may sit in another sub-agent's stream), the prompt
# of that call, the agent's own first and last time, and its last plain text reply.
_REFRESH_AGENT_RUNS = """
    UPDATE agent_run a SET
        launch_event_id = d.launch_id, instructions = d.instructions, result = d.result,
        started_at = d.started, ended_at = d.ended, event_count = d.total
      FROM (
        SELECT r.agent_id, l.event_id AS launch_id,
               jsonb_path_query_first(
                   l.payload,
                   '$.message.content[*] ? (@.type == "tool_use" && @.id == $id).input.prompt',
                   jsonb_build_object('id', r.tool_use_id)) #>> '{}' AS instructions,
               (SELECT jsonb_path_query_first(
                           x.payload, '$.message.content[*] ? (@.type == "text").text') #>> '{}'
                  FROM event x
                 WHERE x.session_id = r.session_id AND x.event_type = 'response'
                   AND split_part(x.raw_object_key, '/', 3) = 'agent-' || r.agent_id
                 ORDER BY x.raw_object_key DESC, x.raw_line_number DESC LIMIT 1) AS result,
               s.started, s.ended, s.total
          FROM agent_run r
          LEFT JOIN LATERAL (
              SELECT e.event_id, e.payload FROM event e
               WHERE e.session_id = r.session_id AND r.tool_use_id IS NOT NULL
                 AND e.tool_use_ids @> ARRAY[r.tool_use_id]
               ORDER BY e.raw_object_key, e.raw_line_number LIMIT 1) l ON TRUE
          LEFT JOIN LATERAL (
              SELECT min(e.occurred_at) AS started, max(e.occurred_at) AS ended, count(*) AS total
                FROM event e
               WHERE e.session_id = r.session_id
                 AND split_part(e.raw_object_key, '/', 3) = 'agent-' || r.agent_id) s ON TRUE
         WHERE r.session_id = %s) d
     WHERE a.session_id = %s AND a.agent_id = d.agent_id
"""


# The edit results not yet turned into file_change rows. The big fields that the indexer does not
# need (the whole old file, the old and new text) are cut out in the database, so they are not
# sent over the connection. A result counts if it names a file and holds a patch list.
_NEW_FILE_RESULTS = """
    SELECT e.event_id,
           e.payload #- '{toolUseResult,originalFile}' #- '{toolUseResult,oldString}'
                      #- '{toolUseResult,newString}'
      FROM event e
     WHERE e.session_id = %s
       AND jsonb_typeof(e.payload #> '{toolUseResult,filePath}') = 'string'
       AND jsonb_typeof(e.payload #> '{toolUseResult,structuredPatch}') = 'array'
       AND NOT EXISTS (SELECT 1 FROM file_change f WHERE f.event_id = e.event_id)
     ORDER BY e.event_id
"""

_INSERT_FILE_CHANGE = """
    INSERT INTO file_change (session_id, event_id, tool_use_id, path, absolute_path, change_type,
                             user_modified, lines_added, lines_removed, hunks)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
    ON CONFLICT (event_id) DO NOTHING
"""

# The call behind each change (the event holding the tool_use with that id) and its tool name.
_LINK_FILE_CHANGE_CALLS = """
    UPDATE file_change f SET call_event_id = c.event_id, tool = c.tool
      FROM (
        SELECT g.file_change_id, l.event_id,
               jsonb_path_query_first(
                   l.payload,
                   '$.message.content[*] ? (@.type == "tool_use" && @.id == $id).name',
                   jsonb_build_object('id', g.tool_use_id)) #>> '{}' AS tool
          FROM file_change g
          JOIN LATERAL (
              SELECT e.event_id, e.payload FROM event e
               WHERE e.session_id = g.session_id
                 AND e.tool_use_ids @> ARRAY[g.tool_use_id]
               ORDER BY e.raw_object_key, e.raw_line_number LIMIT 1) l ON TRUE
         WHERE g.session_id = %s AND g.tool_use_id IS NOT NULL) c
     WHERE f.file_change_id = c.file_change_id
       AND (f.call_event_id IS DISTINCT FROM c.event_id OR f.tool IS DISTINCT FROM c.tool)
"""

_FILE_CHANGE_EVENTS = "SELECT file_change_id, event_id FROM file_change WHERE session_id = %s"
_AGENT_LAUNCHES = (
    "SELECT agent_id, launch_event_id FROM agent_run "
    "WHERE session_id = %s AND launch_event_id IS NOT NULL"
)
_SET_PROMPTS = """
    UPDATE file_change f SET prompt_event_id = v.prompt_id
      FROM unnest(%s::bigint[], %s::bigint[]) AS v(change_id, prompt_id)
     WHERE f.file_change_id = v.change_id AND f.prompt_event_id IS DISTINCT FROM v.prompt_id
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
                        list(entry.tool_use_ids),
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

        That is: which events are on abandoned branches, the compaction rows, the session's
        start time and branch, its model usage and cost, its sub-agent runs and its file changes.
        It reads the session's events, so the result is the same however many chunks were indexed
        in which order, and repeating it changes nothing.
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
            cursor.execute(_REFRESH_MODEL_USAGE, (session_id,))
            cursor.execute(_REFRESH_SESSION_COST, (session_id, session_id))
            cursor.execute(_DELETE_MODEL_COSTS, (session_id,))
            cursor.execute(_REFRESH_MODEL_COSTS, (session_id, session_id))
            cursor.execute(_ENSURE_AGENT_RUNS, (session_id,))
            cursor.execute(_REFRESH_AGENT_RUNS, (session_id, session_id))
            self._refresh_file_changes(cursor, session_id, nodes)
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(
                f"refreshing a session failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _refresh_file_changes(
        cursor: pg8000.dbapi.Cursor, session_id: str, nodes: Sequence[TreeNode]
    ) -> None:
        """Turn new edit results into ``file_change`` rows, then link calls and prompts.

        Part of :meth:`refresh_session`, in its transaction. Results that already have a row are
        not read again, so a long session does not cost more at every job.
        """
        cursor.execute(_NEW_FILE_RESULTS, (session_id,))
        for event_id, entry in cursor.fetchall():
            change = extract_file_change(entry)
            if change is None:  # cannot happen: the query uses the same test
                continue
            cursor.execute(
                _INSERT_FILE_CHANGE,
                (
                    session_id,
                    event_id,
                    change.tool_use_id,
                    change.path,
                    change.absolute_path,
                    change.change_type,
                    change.user_modified,
                    change.lines_added,
                    change.lines_removed,
                    json.dumps(change.hunks),
                ),
            )
        cursor.execute(_LINK_FILE_CHANGE_CALLS, (session_id,))

        cursor.execute(_FILE_CHANGE_EVENTS, (session_id,))
        changes = {int(row[0]): int(row[1]) for row in cursor.fetchall()}
        cursor.execute(_AGENT_LAUNCHES, (session_id,))
        launches = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
        prompts = nearest_prompts(nodes, changes.values(), launches)
        ids = list(changes)
        cursor.execute(_SET_PROMPTS, (ids, [prompts[changes[i]] for i in ids]))

    def save_agent_meta(self, session_id: str, agent_id: str, meta: AgentMeta) -> None:
        """Store a sub-agent's description (creating its run if needed). Safe to repeat."""
        self._run(
            _SAVE_AGENT_META,
            (
                session_id,
                agent_id,
                meta.agent_type,
                meta.description,
                meta.model,
                meta.spawn_depth,
                meta.tool_use_id,
            ),
            "saving a sub-agent description",
        )

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
