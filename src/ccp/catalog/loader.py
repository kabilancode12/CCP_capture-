"""Reading the facts of one chat from the database.

Everything the catalog shows comes from the tables and views of the index (migration 016 and
earlier); nothing is computed from the raw transcript except the transcript files themselves
(see :mod:`ccp.catalog.transcripts`). The connection is only read from.

Queries use one round trip per kind of fact and return plain dictionaries, so the rendering code
needs no database knowledge and can be tested with hand-made data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Row = dict[str, Any]


@dataclass
class ChatData:
    """Every fact the catalog needs about one chat."""

    chat: Row
    sessions: list[Row] = field(default_factory=list)
    decisions: list[Row] = field(default_factory=list)
    test_runs: list[Row] = field(default_factory=list)
    test_cases: list[Row] = field(default_factory=list)
    file_changes: list[Row] = field(default_factory=list)
    commits: list[Row] = field(default_factory=list)
    corrections: list[Row] = field(default_factory=list)
    agents: list[Row] = field(default_factory=list)
    workflows: list[Row] = field(default_factory=list)
    slash_commands: list[Row] = field(default_factory=list)
    usage_by_model: list[Row] = field(default_factory=list)
    cost_by_model: list[Row] = field(default_factory=list)
    cost_total: Row | None = None
    events: list[Row] = field(default_factory=list)


def fetch(connection: Any, sql: str, session_id: str) -> list[Row]:
    """Run a query with the chat id as its only parameter; return the rows as dictionaries."""
    cursor = connection.cursor()
    cursor.execute(sql, (session_id,))
    names = [str(column[0]) for column in cursor.description or []]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


_CHAT = """
    SELECT c.chat_id, c.title, c.account_id, c.account_name, c.account_label, c.project_id,
           c.project_name, c.project_label, c.project_kind, c.folder, c.branch, c.start_commit,
           c.started_at, c.ended_at, c.last_activity_at, c.status, c.is_empty, c.surface,
           c.git_user_name,
           (SELECT a.account_uuid FROM claude_account a WHERE a.account_id = c.account_id)
               AS account_uuid,
           (SELECT p.project_key FROM project p WHERE p.project_id = c.project_id) AS project_key,
           (SELECT p.git_remote FROM project p WHERE p.project_id = c.project_id) AS git_remote
      FROM chat c WHERE c.chat_id = %s
"""

_SESSIONS = """
    SELECT seq, start_source, started_at, ended_at, end_reason, start_commit, end_commit,
           run_key IS NOT NULL AS reported_by_client
      FROM chat_session WHERE session_id = %s ORDER BY seq
"""

_DECISIONS = """
    SELECT decision_id, seq, title, goal, options, chosen, reason, result, tests_summary,
           decided_by, files,
           commit_shas, evidence_lines, status, reviewed_by_kind, reviewer, review_note,
           review_history, model, prompt_version, created_at, updated_at
      FROM decision WHERE session_id = %s ORDER BY seq
"""

_TEST_RUNS = """
    SELECT t.test_run_id, t.command, t.framework, t.passed, t.exit_code, t.tests_passed,
           t.tests_failed, t.tests_skipped, t.output_source,
           COALESCE(r.occurred_at, c.occurred_at) AS occurred_at,
           COALESCE(r.raw_object_key, c.raw_object_key) AS raw_object_key,
           COALESCE(r.raw_line_number, c.raw_line_number) AS raw_line_number
      FROM test_run t
      LEFT JOIN event r ON r.event_id = t.result_event_id
      LEFT JOIN event c ON c.event_id = t.call_event_id
     WHERE t.session_id = %s
     ORDER BY COALESCE(r.occurred_at, c.occurred_at), t.test_run_id
"""

_TEST_CASES = """
    SELECT k.test_run_id, k.position, k.name, k.outcome, k.message
      FROM test_case k JOIN test_run t USING (test_run_id)
     WHERE t.session_id = %s ORDER BY k.test_run_id, k.position
"""

_FILE_CHANGES = """
    SELECT f.file_change_id, f.path, f.tool, f.change_type, f.actor_class, f.user_modified,
           f.lines_added, f.lines_removed, f.file_role, f.tests_added,
           e.occurred_at, e.is_abandoned, e.raw_object_key, e.raw_line_number
      FROM file_change f LEFT JOIN event e ON e.event_id = f.event_id
     WHERE f.session_id = %s ORDER BY e.occurred_at, f.file_change_id
"""

_COMMITS = """
    SELECT commit_sha, method, confidence, matched_lines, total_lines, committed_at
      FROM commit_link WHERE session_id = %s ORDER BY committed_at, commit_sha
"""

_CORRECTIONS = """
    SELECT k.correction_id, k.kind, k.corrected_by, k.detected_by, k.note, cs.seq AS session_seq,
           t.occurred_at AS trigger_at, t.raw_object_key AS trigger_key,
           t.raw_line_number AS trigger_line,
           f.occurred_at AS fix_at, f.raw_object_key AS fix_key, f.raw_line_number AS fix_line
      FROM correction k
      JOIN event t ON t.event_id = k.trigger_event_id
      LEFT JOIN event f ON f.event_id = k.fix_event_id
      LEFT JOIN chat_session cs ON cs.chat_session_id = k.chat_session_id
     WHERE k.session_id = %s ORDER BY t.occurred_at, k.correction_id
"""

_AGENTS = """
    SELECT agent_id, agent_type, description, model, effort, spawn_depth, tool_use_id,
           workflow_run_id, workflow_name, started_at, ended_at, event_count, instructions, result
      FROM agent_run WHERE session_id = %s ORDER BY started_at NULLS LAST, agent_id
"""

_WORKFLOWS = """
    SELECT workflow_run_id, workflow_name, status, agent_count, started_at, ended_at, event_count
      FROM v_workflow_run WHERE session_id = %s ORDER BY started_at NULLS LAST, workflow_run_id
"""

_SLASH_COMMANDS = """
    SELECT event_id, occurred_at, is_abandoned, name, arguments
      FROM v_slash_command WHERE session_id = %s ORDER BY event_id
"""

_USAGE = """
    SELECT model, effort, requested_model, count(*) AS responses,
           sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
           sum(thinking_tokens) AS thinking_tokens, sum(cache_read_tokens) AS cache_read_tokens,
           sum(cache_creation_tokens) AS cache_creation_tokens, sum(thinking_ms) AS thinking_ms
      FROM model_usage WHERE session_id = %s
     GROUP BY model, effort, requested_model
     ORDER BY model NULLS FIRST, effort NULLS FIRST, requested_model NULLS FIRST
"""

_COST = """
    SELECT model, input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
           cache_creation_tokens, web_search_requests, cost_usd
      FROM session_model_cost WHERE session_id = %s ORDER BY model
"""

_COST_TOTAL = """
    SELECT total_cost_usd, has_unknown_model_cost FROM v_session_cost WHERE session_id = %s
"""

# The event types the readable conversation is made of.
_EVENTS = """
    SELECT event_id, event_type, actor, occurred_at, is_abandoned, raw_line_number,
           payload #> '{message,content}' AS content,
           payload #> '{compactMetadata}' AS compact
      FROM event
     WHERE session_id = %s AND split_part(raw_object_key, '/', 3) = 'main'
       AND event_type IN ('prompt', 'slash_command', 'response', 'tool_call', 'thinking',
                          'tool_result', 'compact_boundary', 'agent_message', 'command_output')
     ORDER BY raw_object_key, raw_line_number
"""


def load_chat(connection: Any, session_id: str) -> ChatData | None:
    """Read everything the catalog shows about a chat.

    Args:
        connection: An open database connection (only read from).
        session_id: The chat id.

    Returns:
        The chat's facts, or None if there is no such chat.
    """
    chats = fetch(connection, _CHAT, session_id)
    if not chats:
        return None
    totals = fetch(connection, _COST_TOTAL, session_id)
    return ChatData(
        chat=chats[0],
        sessions=fetch(connection, _SESSIONS, session_id),
        decisions=fetch(connection, _DECISIONS, session_id),
        test_runs=fetch(connection, _TEST_RUNS, session_id),
        test_cases=fetch(connection, _TEST_CASES, session_id),
        file_changes=fetch(connection, _FILE_CHANGES, session_id),
        commits=fetch(connection, _COMMITS, session_id),
        corrections=fetch(connection, _CORRECTIONS, session_id),
        agents=fetch(connection, _AGENTS, session_id),
        workflows=fetch(connection, _WORKFLOWS, session_id),
        slash_commands=fetch(connection, _SLASH_COMMANDS, session_id),
        usage_by_model=fetch(connection, _USAGE, session_id),
        cost_by_model=fetch(connection, _COST, session_id),
        cost_total=totals[0] if totals else None,
        events=fetch(connection, _EVENTS, session_id),
    )


def list_chat_ids(connection: Any) -> tuple[list[str], list[str]]:
    """Every chat id, split into ``(chats with messages, empty chats)``, oldest first."""
    cursor = connection.cursor()
    cursor.execute(
        "SELECT session_id, is_empty FROM session ORDER BY started_at NULLS LAST, session_id"
    )
    rows = cursor.fetchall()
    return [r[0] for r in rows if not r[1]], [r[0] for r in rows if r[1]]


def list_catalog_rows(connection: Any) -> list[Row]:
    """The chats shown in the catalog index (not empty), with the facts the folder names need."""
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT c.chat_id, c.title, c.started_at, c.last_activity_at, c.account_id, c.account_name,
               c.account_label, c.project_id, c.project_name, c.project_label,
               (SELECT a.account_uuid FROM claude_account a WHERE a.account_id = c.account_id)
                   AS account_uuid
          FROM chat c WHERE NOT c.is_empty ORDER BY c.started_at NULLS LAST, c.chat_id
        """
    )
    names = [str(column[0]) for column in cursor.description or []]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
