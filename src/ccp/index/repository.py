"""The indexer's use of the database: the job queue and the event rows.

Every method opens a short-lived connection, does its work in one transaction and closes the
connection, like :mod:`ccp.ingest.repository`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import pg8000.dbapi

from ccp.db.connection import connect
from ccp.index.agents import AgentMeta
from ccp.index.corrections import refresh_corrections
from ccp.index.filechanges import extract_file_change
from ccp.index.fileroles import file_role
from ccp.index.identity import refresh_identity
from ccp.index.parser import ParsedEntry
from ccp.index.sessions import idle_minutes_from_environment, refresh_chat_sessions
from ccp.index.shellfiles import files_changed_by
from ccp.index.testoutputs import MISSING, OutputStore, read_test_output
from ccp.index.testparsers import parse_test_output
from ccp.index.testruns import detect_test_runner, exit_code_of, outcome
from ccp.index.tree import TreeNode, abandoned_event_ids, nearest_prompts
from ccp.settings import DatabaseSettings

logger = logging.getLogger("ccp.index")

# A job that failed is tried again this many times in all, then left as 'failed'.
MAX_ATTEMPTS = 3

# A job that has been 'running' this long is taken to belong to a worker that died: the next
# worker that looks for work puts it back in the queue. Indexing even a long session takes well
# under a minute, so this is far above any real run (and a run that does last longer is harmless:
# work on one session is serialised, see LOCK_SESSION, and indexing again adds nothing).
STALE_JOB_MINUTES = 30

# SQLSTATE of a unique violation.
_UNIQUE_VIOLATION = "23505"

# Held for the rest of the transaction by everything that writes a session's index (storing a
# chunk, refreshing the derived rows, the rebuild's reset). Two workers on the same session (a new
# chunk queued a job while the old one was running) then take turns instead of failing on each
# other's rows. Different sessions do not wait for each other.
LOCK_SESSION = "SELECT pg_advisory_xact_lock(hashtext(%s))"

# Index jobs whose worker seems to have died: 'running' for longer than the timeout. A job taken
# before migration 014 has no start time; its creation time stands in. Locked, so that two workers
# do not both handle the same job.
_STALE_JOBS = """
    SELECT job_id, attempts FROM job
     WHERE job_type = 'index_session' AND status = 'running'
       AND COALESCE(started_at, created_at) < now() - %s * interval '1 minute'
     ORDER BY job_id
       FOR UPDATE SKIP LOCKED
"""

# Take the oldest queued index job. FOR UPDATE SKIP LOCKED lets several workers run at once:
# each takes a different job, and none waits for another.
_CLAIM_JOB = """
    UPDATE job SET status = 'running', attempts = attempts + 1, started_at = now()
     WHERE job_id = (
        SELECT job_id FROM job
         WHERE job_type = 'index_session' AND status = 'queued'
         ORDER BY created_at, job_id
           FOR UPDATE SKIP LOCKED
         LIMIT 1)
    RETURNING job_id, session_id, attempts
"""

_FINISH_JOB = "UPDATE job SET status = 'done', finished_at = now() WHERE job_id = %s"

# Put a job back in the queue, unless it has used its attempts or the session already has a
# queued job (a new chunk arrived meanwhile; that job will index everything anyway).
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

# Events (and thinking rows) are sent this many at a time: one statement per batch instead of one
# per row, which is what makes a long chunk fast to store.
_BATCH_SIZE = 1000

# The tool call ids of one event travel as one text, joined with this character, and are split
# again in the database. Tool call ids are made of letters, digits, "_" and "-", so they never
# contain it. An event without ids gives the empty text, which splits into an empty array.
_ID_SEPARATOR = "\x1f"

# Insert a batch of events, one array per column. In the order of the chunk, so the database's own
# ids grow with the line number as before. If an event exists already (same object, same line)
# nothing changes, but its id is still returned, so re-indexing is safe and the thinking rows can
# be attached.
_INSERT_EVENTS = """
    INSERT INTO event (session_id, entry_uuid, parent_uuid, event_type, actor, occurred_at,
                       payload, raw_object_key, raw_line_number, tool_use_ids)
    SELECT %s, v.entry_uuid, v.parent_uuid, v.event_type, v.actor, v.occurred_at,
           v.payload::jsonb, %s, v.line_number, string_to_array(v.tool_use_ids, chr(31))
      FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::timestamptz[],
                  %s::text[], %s::integer[], %s::text[])
           WITH ORDINALITY AS v(entry_uuid, parent_uuid, event_type, actor, occurred_at,
                                payload, line_number, tool_use_ids, position)
     ORDER BY v.position
    ON CONFLICT (raw_object_key, raw_line_number)
    DO UPDATE SET session_id = EXCLUDED.session_id
    RETURNING event_id, raw_line_number
"""

# Insert a batch of thinking blocks, one array per column.
_INSERT_THINKING = """
    INSERT INTO thinking (event_id, block_index, text, is_redacted)
    SELECT * FROM unnest(%s::bigint[], %s::integer[], %s::text[], %s::boolean[])
    ON CONFLICT (event_id, block_index) DO NOTHING
"""


# The events the tree rule needs. A prompt counts as the developer's only if a human wrote it (a
# typed prompt or a slash command) and Claude Code did not inject it (isMeta; events indexed before
# the parser told these apart are still marked 'prompt').
_TREE_NODES = """
    SELECT event_id, entry_uuid, parent_uuid, payload->>'logicalParentUuid',
           event_type IN ('prompt', 'slash_command') AND actor = 'human'
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
# with the same usage, so the first entry of each message is kept, and it also names the effort and
# the model that was asked for. The thinking time is the sum over the message's distinct entries
# (an entry is sometimes written twice with one uuid; each thinking block has its own time). A row
# that exists already is brought up to date, so chats indexed before these columns get them.
_REFRESH_MODEL_USAGE = """
    WITH entries AS (
        SELECT session_id, COALESCE(payload #>> '{message,id}', entry_uuid) AS message_id,
               event_id, COALESCE(entry_uuid, event_id::text) AS entry_key, raw_object_key,
               raw_line_number, payload, json_bigint(payload -> 'thinkingDurationMs') AS thinking_ms
          FROM event
         WHERE session_id = %s AND event_type IN ('response', 'tool_call', 'thinking')
           AND jsonb_typeof(payload #> '{message,usage}') = 'object'
           AND COALESCE(payload #>> '{message,id}', entry_uuid) IS NOT NULL
    ), first_entry AS (
        SELECT DISTINCT ON (message_id) * FROM entries
         ORDER BY message_id, raw_object_key, raw_line_number
    ), thinking AS (
        SELECT message_id, sum(thinking_ms)::bigint AS thinking_ms FROM (
            SELECT DISTINCT ON (message_id, entry_key) message_id, thinking_ms FROM entries
             WHERE thinking_ms IS NOT NULL
             ORDER BY message_id, entry_key, raw_object_key, raw_line_number) distinct_entries
         GROUP BY message_id
    )
    INSERT INTO model_usage (session_id, message_id, event_id, model, input_tokens, output_tokens,
                             thinking_tokens, cache_read_tokens, cache_creation_tokens, effort,
                             requested_model, thinking_ms)
    SELECT f.session_id, f.message_id, f.event_id,
           f.payload #>> '{message,model}',
           json_bigint(f.payload #> '{message,usage,input_tokens}'),
           json_bigint(f.payload #> '{message,usage,output_tokens}'),
           json_bigint(f.payload #> '{message,usage,output_tokens_details,thinking_tokens}'),
           json_bigint(f.payload #> '{message,usage,cache_read_input_tokens}'),
           json_bigint(f.payload #> '{message,usage,cache_creation_input_tokens}'),
           CASE WHEN jsonb_typeof(f.payload -> 'effort') = 'string' THEN f.payload ->> 'effort' END,
           CASE WHEN jsonb_typeof(f.payload -> 'requestedModel') = 'string'
                THEN f.payload ->> 'requestedModel' END,
           t.thinking_ms
      FROM first_entry f LEFT JOIN thinking t USING (message_id)
    ON CONFLICT (session_id, message_id) DO UPDATE SET
        effort = EXCLUDED.effort, requested_model = EXCLUDED.requested_model,
        thinking_ms = EXCLUDED.thinking_ms
     WHERE (model_usage.effort, model_usage.requested_model, model_usage.thinking_ms)
           IS DISTINCT FROM (EXCLUDED.effort, EXCLUDED.requested_model, EXCLUDED.thinking_ms)
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
                           tool_use_id, effort, workflow_run_id, workflow_name)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (session_id, agent_id) DO UPDATE SET
        agent_type = EXCLUDED.agent_type, description = EXCLUDED.description,
        model = EXCLUDED.model, spawn_depth = EXCLUDED.spawn_depth,
        tool_use_id = EXCLUDED.tool_use_id, effort = EXCLUDED.effort,
        workflow_run_id = EXCLUDED.workflow_run_id, workflow_name = EXCLUDED.workflow_name
"""

# The reasoning effort a sub-agent ran with: the highest one named by its own assistant entries
# (an agent can change effort between turns). The description file does not name it (so far).
_REFRESH_AGENT_EFFORT = """
    UPDATE agent_run a SET effort = e.effort
      FROM (SELECT substr(split_part(raw_object_key, '/', 3), 7) AS agent_id,
                   (ARRAY['low', 'medium', 'high', 'xhigh', 'max'])[
                       max(array_position(ARRAY['low', 'medium', 'high', 'xhigh', 'max'],
                                          payload ->> 'effort'))] AS effort
              FROM event
             WHERE session_id = %s AND split_part(raw_object_key, '/', 3) ~ '^agent-'
               AND event_type IN ('response', 'tool_call', 'thinking')
             GROUP BY 1) e
     WHERE a.session_id = %s AND a.agent_id = e.agent_id AND e.effort IS NOT NULL
       AND a.effort IS DISTINCT FROM e.effort
"""

# The Workflow run of a sub-agent. The capture client writes it into the description (it knows the
# folder subagents/workflows/<run id>/). Chats captured before that have a description without it:
# a "workflow-subagent" then belongs to the latest Workflow run launched at or before its first
# entry (a run is launched by a tool result naming its runId, see v_workflow_run). The run's name
# comes from that launching result.
_INFER_WORKFLOW_RUN = """
    UPDATE agent_run a SET workflow_run_id = (
        SELECT l.run_id FROM (
            SELECT payload #>> '{toolUseResult,runId}' AS run_id, min(occurred_at) AS launched_at
              FROM event
             WHERE session_id = a.session_id AND event_type = 'tool_result'
               AND payload #>> '{toolUseResult,runId}' IS NOT NULL AND occurred_at IS NOT NULL
             GROUP BY 1) l
         WHERE l.launched_at <= a.started_at
         ORDER BY l.launched_at DESC, l.run_id DESC LIMIT 1)
     WHERE a.session_id = %s AND a.workflow_run_id IS NULL AND a.agent_type = 'workflow-subagent'
       AND a.started_at IS NOT NULL
"""

_NAME_WORKFLOW_RUN = """
    UPDATE agent_run a SET workflow_name = w.name
      FROM (SELECT payload #>> '{toolUseResult,runId}' AS run_id,
                   max(payload #>> '{toolUseResult,workflowName}') AS name
              FROM event
             WHERE session_id = %s AND event_type = 'tool_result'
               AND payload #>> '{toolUseResult,runId}' IS NOT NULL
             GROUP BY 1) w
     WHERE a.session_id = %s AND a.workflow_run_id = w.run_id AND a.workflow_name IS NULL
       AND w.name IS NOT NULL
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
# of that call, the agent's own first and last time, and its last plain text reply. If the
# description named no model, the model comes from the result of the launching call
# ("resolvedModel"); the description is stored again before every refresh, so its model wins.
_REFRESH_AGENT_RUNS = """
    UPDATE agent_run a SET
        launch_event_id = d.launch_id, instructions = d.instructions, result = d.result,
        started_at = d.started, ended_at = d.ended, event_count = d.total,
        model = COALESCE(a.model, d.resolved_model)
      FROM (
        SELECT r.agent_id, l.event_id AS launch_id,
               jsonb_path_query_first(
                   l.payload,
                   '$.message.content[*] ? (@.type == "tool_use" && @.id == $id).input.prompt',
                   jsonb_build_object('id', r.tool_use_id)) #>> '{}' AS instructions,
               (SELECT NULLIF(x.payload #>> '{toolUseResult,resolvedModel}', '')
                  FROM event x
                 WHERE x.session_id = r.session_id AND r.tool_use_id IS NOT NULL
                   AND x.event_type = 'tool_result' AND x.tool_use_ids @> ARRAY[r.tool_use_id]
                   AND jsonb_typeof(x.payload #> '{toolUseResult,resolvedModel}') = 'string'
                 ORDER BY x.raw_object_key, x.raw_line_number LIMIT 1) AS resolved_model,
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
                 AND e.event_type = 'tool_call' AND e.tool_use_ids @> ARRAY[r.tool_use_id]
               ORDER BY e.raw_object_key, e.raw_line_number LIMIT 1) l ON TRUE
          LEFT JOIN LATERAL (
              SELECT min(e.occurred_at) AS started, max(e.occurred_at) AS ended, count(*) AS total
                FROM event e
               WHERE e.session_id = r.session_id
                 AND split_part(e.raw_object_key, '/', 3) = 'agent-' || r.agent_id) s ON TRUE
         WHERE r.session_id = %s) d
     WHERE a.session_id = %s AND a.agent_id = d.agent_id
"""


# The edit results not yet turned into file_change rows (or whose row has no file role yet: a row
# stored before roles existed is completed, see _INSERT_FILE_CHANGE). The big fields that the
# indexer does not need (the whole old file, the old and new text) are cut out in the database, so
# they are not sent over the connection. A result counts if it names a file and holds a patch list.
_NEW_FILE_RESULTS = """
    SELECT e.event_id,
           e.payload #- '{toolUseResult,originalFile}' #- '{toolUseResult,oldString}'
                      #- '{toolUseResult,newString}'
      FROM event e
     WHERE e.session_id = %s
       AND jsonb_typeof(e.payload #> '{toolUseResult,filePath}') = 'string'
       AND jsonb_typeof(e.payload #> '{toolUseResult,structuredPatch}') = 'array'
       AND NOT EXISTS (SELECT 1 FROM file_change f
                        WHERE f.event_id = e.event_id AND f.file_role IS NOT NULL)
     ORDER BY e.event_id
"""

_INSERT_FILE_CHANGE = """
    INSERT INTO file_change (session_id, event_id, tool_use_id, path, absolute_path, change_type,
                             user_modified, lines_added, lines_removed, hunks, file_role,
                             tests_added)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
    ON CONFLICT (event_id, path) DO UPDATE SET
        file_role = EXCLUDED.file_role, tests_added = EXCLUDED.tests_added
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
                 AND e.event_type = 'tool_call' AND e.tool_use_ids @> ARRAY[g.tool_use_id]
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


# Shell calls (with the result that answered them) that are not yet test runs. The result's facts
# are read in the database so that its (possibly huge) output is not sent: whether it was an
# error, the exit code that a failure starts with, whether it was interrupted, and whether the
# call was refused (a refused call never ran).
_NEW_SHELL_CALLS = """
    SELECT c.event_id, c.payload #> '{message,content}', r.event_id,
           jsonb_path_exists(r.payload,
               '$.message.content[*] ? (@.type == "tool_result" && @.is_error == true)'),
           substring(r.payload #>> '{message,content,0,content}' FROM '^Exit code [0-9]+'),
           COALESCE(r.payload #>> '{toolUseResult,interrupted}' = 'true', FALSE),
           r.payload ? 'toolDenialKind'
      FROM event c
      JOIN event r ON r.session_id = c.session_id AND r.event_type = 'tool_result'
                  AND r.tool_use_ids && c.tool_use_ids
     WHERE c.session_id = %s AND c.event_type = 'tool_call'
       AND jsonb_path_exists(c.payload,
               '$.message.content[*] ? (@.type == "tool_use"
                   && (@.name == "Bash" || @.name == "PowerShell"))')
       AND NOT EXISTS (SELECT 1 FROM test_run t WHERE t.call_event_id = c.event_id)
     ORDER BY c.event_id
"""

# Shell calls whose result has no file_change row yet: the files a command writes, changes or
# removes are read from its text (see ccp.index.shellfiles). A call that changes no file is looked
# at again at every refresh, like a call that is no test run. Refused calls never ran. The call's
# working directory is cut off the paths.
_NEW_SHELL_FILE_CALLS = """
    SELECT c.event_id, c.payload #> '{message,content}', r.event_id, c.payload ->> 'cwd',
           r.payload ? 'toolDenialKind'
      FROM event c
      JOIN event r ON r.session_id = c.session_id AND r.event_type = 'tool_result'
                  AND r.tool_use_ids && c.tool_use_ids
     WHERE c.session_id = %s AND c.event_type = 'tool_call'
       AND jsonb_path_exists(c.payload,
               '$.message.content[*] ? (@.type == "tool_use"
                   && (@.name == "Bash" || @.name == "PowerShell"))')
       AND NOT EXISTS (SELECT 1 FROM file_change f WHERE f.event_id = r.event_id)
     ORDER BY c.event_id
"""

_INSERT_SHELL_FILE_CHANGE = """
    INSERT INTO file_change (session_id, event_id, tool_use_id, call_event_id, tool, path,
                             absolute_path, change_type, actor_class, file_role)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ai_command', %s)
    ON CONFLICT (event_id, path) DO NOTHING
"""

_SHELL_CHANGE_TYPES = {"created": "added", "modified": "modified", "deleted": "deleted"}


def _shell_file_rows(blocks: object, cwd: str | None) -> list[tuple[str, str, str, str, str]]:
    """For each file a shell call changes: tool use id, tool, path, absolute path, change type."""
    rows: list[tuple[str, str, str, str, str]] = []
    for block in blocks if isinstance(blocks, list) else ():
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        arguments = block.get("input")
        command = arguments.get("command") if isinstance(arguments, dict) else None
        tool_use_id = block.get("id")
        if not isinstance(command, str) or not isinstance(tool_use_id, str):
            continue
        base = (cwd or "").rstrip("/")
        for item in files_changed_by(command):
            if item.path.startswith("("):  # a project scaffold names no file
                continue
            absolute = item.path if item.path.startswith("/") else f"{base}/{item.path}"
            relative = (
                absolute[len(base) + 1 :] if base and absolute.startswith(base + "/") else item.path
            )
            rows.append(
                (tool_use_id, str(block.get("name", "Bash")), relative, absolute,
                 _SHELL_CHANGE_TYPES[item.change])
            )  # fmt: skip
    return rows


_INSERT_TEST_RUN = """
    INSERT INTO test_run (session_id, call_event_id, result_event_id, tool_use_id, command,
                          framework, passed, exit_code)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (call_event_id) DO NOTHING
"""

# Test runs whose output has not been read yet, or whose output file was not there last time, with
# the result's content and the file it names (if any). Reading is done once per run.
_PENDING_OUTPUTS = """
    SELECT t.test_run_id, t.framework, r.payload #> '{message,content}',
           r.payload #>> '{toolUseResult,persistedOutputPath}'
      FROM test_run t LEFT JOIN event r ON r.event_id = t.result_event_id
     WHERE t.session_id = %s AND (t.output_source IS NULL OR t.output_source = 'missing')
     ORDER BY t.test_run_id
"""

_SET_TEST_OUTPUT = """
    UPDATE test_run SET tests_passed = %s, tests_failed = %s, tests_skipped = %s,
                        output_source = %s
     WHERE test_run_id = %s
"""
_DELETE_TEST_CASES = "DELETE FROM test_case WHERE test_run_id = %s"
_INSERT_TEST_CASES = """
    INSERT INTO test_case (test_run_id, position, name, outcome, message)
    SELECT %s, v.position, v.name, v.outcome, v.message
      FROM unnest(%s::integer[], %s::text[], %s::text[], %s::text[])
           AS v(position, name, outcome, message)
"""

# Commands longer than this are cut: a test command is short, and a long one is a script.
MAX_COMMAND_LENGTH = 2000


def _shell_test_command(block: object) -> tuple[str, str, str] | None:
    """For a shell ``tool_use`` block that starts a test runner: ``(call id, command, runner)``."""
    if not isinstance(block, dict) or block.get("type") != "tool_use":
        return None
    if block.get("name") not in ("Bash", "PowerShell"):
        return None
    arguments = block.get("input")
    command = arguments.get("command") if isinstance(arguments, dict) else None
    tool_use_id = block.get("id")
    if not isinstance(command, str) or not isinstance(tool_use_id, str):
        return None
    runner = detect_test_runner(command)
    return (tool_use_id, command, runner) if runner else None


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

    def __init__(self, settings: DatabaseSettings, idle_minutes: int | None = None) -> None:
        """Remember the connection settings; nothing is connected yet.

        Args:
            settings: The database.
            idle_minutes: The gap in a chat's transcript that starts a new session when sessions
                are inferred. Default: ``CCP_SESSION_IDLE_MINUTES`` or 30.
        """
        self._settings = settings
        self._idle_minutes = (
            idle_minutes if idle_minutes is not None else idle_minutes_from_environment()
        )

    def claim_job(self) -> IndexJob | None:
        """Take the oldest queued index job and mark it running; None if the queue is empty.

        In the same transaction, and first, jobs whose worker seems to have died are put back in
        the queue (see :meth:`_recover_stale_jobs`), so a crashed worker's job is not lost.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            self._recover_stale_jobs(cursor)
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

        The job is queued again if it has attempts left and the session has no other queued job;
        otherwise it is left as ``failed``. If a queued job for the session appears at the very
        moment it is put back (the API queued one for a new chunk), this job is marked ``done``
        instead: the new job indexes the session anyway.

        Returns:
            True if the job was queued again, False if it is not.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            requeued = self._retry(cursor, job_id)
            if requeued is None:
                cursor.execute(_FINISH_JOB, (job_id,))
            elif not requeued:
                cursor.execute(_FAIL_JOB, (job_id,))
            connection.commit()
            return requeued is True
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"failing a job failed: {type(error).__name__}") from error
        finally:
            connection.close()

    @classmethod
    def _recover_stale_jobs(cls, cursor: pg8000.dbapi.Cursor) -> None:
        """Put the jobs of workers that died back in the queue.

        Part of :meth:`claim_job`, in its transaction. A job that has been ``running`` for longer
        than :data:`STALE_JOB_MINUTES` is queued again if it has attempts left; if the session
        already has a queued job, that job covers it and this one is marked ``done``; a job with
        no attempts left is marked ``failed``.
        """
        cursor.execute(_STALE_JOBS, (STALE_JOB_MINUTES,))
        for job_id, attempts in cursor.fetchall():
            requeued = cls._retry(cursor, job_id)
            if requeued:
                logger.warning("job %s had stopped while running; queued again", job_id)
            elif attempts >= MAX_ATTEMPTS:
                logger.warning("job %s had stopped while running; no attempts left", job_id)
                cursor.execute(_FAIL_JOB, (job_id,))
            else:  # the session already has a queued job, which covers this one
                cursor.execute(_FINISH_JOB, (job_id,))

    @staticmethod
    def _retry(cursor: pg8000.dbapi.Cursor, job_id: int) -> bool | None:
        """Try to put a job back in the queue (see ``_RETRY_JOB``).

        At most one index job per session may be queued (migration 002). The statement checks
        that, but the API can still queue a job for the session between the check and the
        update; the database then refuses the update, which is undone here.

        Returns:
            True if the job was queued again, False if it was not allowed (no attempts left, or
            the session already has a queued job), None if a queued job appeared meanwhile.
        """
        cursor.execute("SAVEPOINT retry_job")
        try:
            cursor.execute(_RETRY_JOB, (job_id, MAX_ATTEMPTS))
        except pg8000.dbapi.DatabaseError as error:
            if _sqlstate(error) != _UNIQUE_VIOLATION:
                raise
            cursor.execute("ROLLBACK TO SAVEPOINT retry_job")
            return None
        requeued = bool(cursor.rowcount == 1)
        cursor.execute("RELEASE SAVEPOINT retry_job")
        return requeued

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

        The events are sent in batches of :data:`_BATCH_SIZE`, one statement per batch. The
        session's lock (:data:`LOCK_SESSION`) is held, so another worker on the same session waits.

        Args:
            session_id: The session the chunk belongs to.
            object_key: The storage key of the chunk.
            entries: ``(line number, entry)`` pairs; line numbers start at 1 within the chunk.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(LOCK_SESSION, (session_id,))
            for start in range(0, len(entries), _BATCH_SIZE):
                batch = entries[start : start + _BATCH_SIZE]
                event_ids = self._insert_events(cursor, session_id, object_key, batch)
                self._insert_thinking(cursor, batch, event_ids)
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(f"saving events failed: {type(error).__name__}") from error
        finally:
            connection.close()

    @staticmethod
    def _insert_events(
        cursor: pg8000.dbapi.Cursor,
        session_id: str,
        object_key: str,
        batch: Sequence[tuple[int, ParsedEntry]],
    ) -> dict[int, int]:
        """Insert a batch of events with one statement. Part of :meth:`save_chunk`.

        Returns:
            The event id of each line number in the batch (also for events that existed already).
        """
        entries = [entry for _, entry in batch]
        cursor.execute(
            _INSERT_EVENTS,
            (
                session_id,
                object_key,
                [entry.entry_uuid for entry in entries],
                [entry.parent_uuid for entry in entries],
                [entry.event_type for entry in entries],
                [entry.actor for entry in entries],
                [entry.occurred_at for entry in entries],
                [json.dumps(entry.payload, ensure_ascii=False) for entry in entries],
                [line_number for line_number, _ in batch],
                [_ID_SEPARATOR.join(entry.tool_use_ids) for entry in entries],
            ),
        )
        event_ids = {int(row[1]): int(row[0]) for row in cursor.fetchall()}
        if len(event_ids) != len(batch):  # cannot happen: RETURNING yields every row
            raise IndexRepositoryError("an event insert returned no id")
        return event_ids

    @staticmethod
    def _insert_thinking(
        cursor: pg8000.dbapi.Cursor,
        batch: Sequence[tuple[int, ParsedEntry]],
        event_ids: dict[int, int],
    ) -> None:
        """Insert the thinking blocks of a batch of events with one statement (if there are any)."""
        blocks = [
            (event_ids[line_number], block)
            for line_number, entry in batch
            for block in entry.thinking
        ]
        if not blocks:
            return
        cursor.execute(
            _INSERT_THINKING,
            (
                [event_id for event_id, _ in blocks],
                [block.block_index for _, block in blocks],
                [block.text for _, block in blocks],
                [block.is_redacted for _, block in blocks],
            ),
        )

    def refresh_session(self, session_id: str) -> None:
        """Recompute everything that is derived from a session's events, in one transaction.

        That is: which events are on abandoned branches, the compaction rows, the session's
        start time and branch, its model usage and cost, its sub-agent runs, file changes and
        test runs, its account, project, title and sessions, and its corrections.
        It reads the session's events, so the result is the same however many chunks were indexed
        in which order, and repeating it changes nothing. The session's lock
        (:data:`LOCK_SESSION`) is held, so two refreshes of one session never run at once.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(LOCK_SESSION, (session_id,))
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
            cursor.execute(_REFRESH_AGENT_EFFORT, (session_id, session_id))
            cursor.execute(_INFER_WORKFLOW_RUN, (session_id,))
            cursor.execute(_NAME_WORKFLOW_RUN, (session_id, session_id))
            self._refresh_file_changes(cursor, session_id, nodes)
            self._refresh_test_runs(cursor, session_id)
            refresh_identity(cursor, session_id)
            refresh_chat_sessions(cursor, session_id, self._idle_minutes)
            refresh_corrections(cursor, session_id)
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
                    change.file_role,
                    change.tests_added,
                ),
            )
        cursor.execute(_NEW_SHELL_FILE_CALLS, (session_id,))
        for call_id, blocks, result_id, cwd, denied in cursor.fetchall():
            if denied:
                continue
            for tool_use_id, tool, path, absolute, change_type in _shell_file_rows(blocks, cwd):
                cursor.execute(
                    _INSERT_SHELL_FILE_CHANGE,
                    (session_id, result_id, tool_use_id, call_id, tool, path, absolute,
                     change_type, file_role(path)),
                )  # fmt: skip
        cursor.execute(_LINK_FILE_CHANGE_CALLS, (session_id,))

        cursor.execute(_FILE_CHANGE_EVENTS, (session_id,))
        changes = {int(row[0]): int(row[1]) for row in cursor.fetchall()}
        cursor.execute(_AGENT_LAUNCHES, (session_id,))
        launches = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
        prompts = nearest_prompts(nodes, changes.values(), launches)
        ids = list(changes)
        cursor.execute(_SET_PROMPTS, (ids, [prompts[changes[i]] for i in ids]))

    @staticmethod
    def _refresh_test_runs(cursor: pg8000.dbapi.Cursor, session_id: str) -> None:
        """Turn shell calls that ran a test runner into ``test_run`` rows.

        Part of :meth:`refresh_session`, in its transaction. A call is looked at once, when its
        result is there: a call without a result is not a run yet, and a call that is not a test
        run is simply looked at again next time (these are few).
        """
        cursor.execute(_NEW_SHELL_CALLS, (session_id,))
        for (
            call_id,
            blocks,
            result_id,
            is_error,
            exit_text,
            interrupted,
            denied,
        ) in cursor.fetchall():
            if denied:
                continue
            for block in blocks if isinstance(blocks, list) else ():
                found = _shell_test_command(block)
                if found is None:
                    continue
                tool_use_id, command, runner = found
                passed, code = outcome(
                    is_error=is_error, exit_code=exit_code_of(exit_text), interrupted=interrupted
                )
                cursor.execute(
                    _INSERT_TEST_RUN,
                    (
                        session_id,
                        call_id,
                        result_id,
                        tool_use_id,
                        command[:MAX_COMMAND_LENGTH],
                        runner,
                        passed,
                        code,
                    ),
                )
                break  # one run per call

    def refresh_test_outputs(self, session_id: str, store: OutputStore) -> None:
        """Read the output of the session's test runs and store the counts and test cases.

        A run is read once: from its result, or from the tool-result file the result names. If that
        file has not been uploaded yet the run is marked ``missing`` and tried again at the next
        refresh. The files are read before the session's lock is taken, then everything is stored
        in one transaction.
        """
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(_PENDING_OUTPUTS, (session_id,))
            pending = cursor.fetchall()
        except (pg8000.dbapi.Error, OSError) as error:
            raise IndexRepositoryError(
                f"reading test runs failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()
        results = []
        for run_id, framework, content, persisted_path in pending:
            text, source = (
                read_test_output(store, session_id, content, persisted_path)
                if content is not None
                else (None, MISSING)
            )
            summary = parse_test_output(framework, text) if text is not None else None
            results.append((run_id, summary, source))
        if not results:
            return
        connection = self._open()
        try:
            cursor = connection.cursor()
            cursor.execute(LOCK_SESSION, (session_id,))
            for run_id, summary, source in results:
                cursor.execute(
                    _SET_TEST_OUTPUT,
                    (
                        summary.passed if summary else None,
                        summary.failed if summary else None,
                        summary.skipped if summary else None,
                        source,
                        run_id,
                    ),
                )
                cursor.execute(_DELETE_TEST_CASES, (run_id,))
                if summary and summary.cases:
                    cursor.execute(
                        _INSERT_TEST_CASES,
                        (
                            run_id,
                            list(range(1, len(summary.cases) + 1)),
                            [_clean(case.name) for case in summary.cases],
                            [case.outcome for case in summary.cases],
                            [_clean(case.message) for case in summary.cases],
                        ),
                    )
            connection.commit()
        except (pg8000.dbapi.Error, OSError) as error:
            connection.rollback()
            raise IndexRepositoryError(
                f"storing test results failed: {type(error).__name__}"
            ) from error
        finally:
            connection.close()

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
                meta.effort,
                meta.workflow_run_id,
                meta.workflow_name,
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


def _clean(text: str | None) -> str | None:
    """Text for the database: PostgreSQL cannot store the NUL character."""
    return text.replace("\x00", "\ufffd") if text is not None else None


def _sqlstate(error: pg8000.dbapi.DatabaseError) -> str | None:
    """The SQLSTATE code of a database error (pg8000 puts the server's fields in a dict)."""
    fields = error.args[0] if error.args else None
    code = fields.get("C") if isinstance(fields, dict) else None
    return code if isinstance(code, str) else None
