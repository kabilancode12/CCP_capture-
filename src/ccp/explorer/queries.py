# ruff: noqa: S608  (the f-strings only join constant SQL fragments)
"""What the explorer website reads from the database, one function per page or tab.

Each function takes an open connection and returns plain rows (``dict`` of column name to value),
which :mod:`ccp.explorer.pages` turns into HTML. Nothing here decides who may see what: the
application checks access before it calls these.

The chat-level views of migration 016 are used where they exist: ``chat`` (a chat with its account
and project), ``d_usage`` (tokens per model), ``v_workflow_run`` and ``v_slash_command``.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any

import pg8000.dbapi

Row = dict[str, Any]

# Events shown on the Conversation tab, from the main transcript only (sub-agents have their own
# tab). Thinking has its own tab; everything else (attachments, hook summaries ...) is noise here.
CONVERSATION_TYPES = (
    "prompt",
    "response",
    "tool_call",
    "tool_result",
    "slash_command",
    "command_output",
    "agent_message",
    "compaction_summary",
    "compact_boundary",
)

# How many conversation entries one page shows.
CONVERSATION_PAGE_SIZE = 400

# Most chats a project page lists, newest first.
CHAT_LIST_LIMIT = 500

_MAIN = "split_part(e.raw_object_key, '/', 3) = 'main'"

_HOME = """
    SELECT c.account_id, max(c.account_label) AS account_label,
           max(c.account_name) AS account_name, max(c.account_email) AS account_email,
           c.project_id, max(c.project_label) AS project_label,
           max(c.project_name) AS project_name, max(c.project_kind) AS project_kind,
           max(c.folder) AS folder,
           count(*) FILTER (WHERE NOT c.is_empty) AS chats,
           count(*) FILTER (WHERE c.is_empty) AS empty_chats,
           COALESCE(sum((SELECT sum(d.total_tokens) FROM d_usage d
                          WHERE d.session_id = c.chat_id)), 0)::bigint AS tokens,
           max(c.last_activity_at) AS last_activity_at
      FROM chat c
     GROUP BY c.account_id, c.project_id
     ORDER BY max(c.account_label) NULLS LAST, max(c.account_email) NULLS LAST, c.account_id,
              max(c.last_activity_at) DESC
"""

_CHAT_LIST = f"""
    SELECT c.chat_id, c.title, c.account_id, c.account_label, c.account_name, c.account_email,
           c.project_id, c.project_label, c.project_name, c.project_kind, c.folder, c.branch,
           c.started_at, c.ended_at, c.last_activity_at, c.status, c.is_empty, c.surface,
           c.git_user_name, c.session_count AS sessions,
           (SELECT count(*) FROM event e
             WHERE e.session_id = c.chat_id AND e.event_type = 'prompt' AND e.actor = 'human'
               AND NOT e.is_abandoned AND {_MAIN}) AS prompts,
           (SELECT COALESCE(sum(d.total_tokens), 0)::bigint FROM d_usage d
             WHERE d.session_id = c.chat_id) AS tokens,
           (SELECT COALESCE(sum(r.tests_passed), 0)::bigint FROM test_run r
             WHERE r.session_id = c.chat_id) AS tests_passed,
           (SELECT COALESCE(sum(r.tests_failed), 0)::bigint FROM test_run r
             WHERE r.session_id = c.chat_id) AS tests_failed,
           (SELECT count(*) FROM decision d
             WHERE d.session_id = c.chat_id AND d.status <> 'rejected') AS decisions,
           (SELECT count(*) FROM correction k WHERE k.session_id = c.chat_id) AS corrections
      FROM chat c
     WHERE {{where}}
     ORDER BY COALESCE(c.started_at, c.last_activity_at) DESC NULLS LAST, c.chat_id
     LIMIT {CHAT_LIST_LIMIT}
"""

_CONVERSATION = f"""
    SELECT e.event_id, e.event_type, e.actor, e.occurred_at, e.is_abandoned,
           CASE WHEN e.event_type = 'tool_result' THEN NULL
                ELSE e.payload #> '{{message,content}}' END AS content,
           CASE WHEN e.event_type = 'tool_result' THEN jsonb_path_exists(
                    e.payload, '$.message.content[*] ? (@.is_error == true)') END AS is_error,
           e.payload ->> 'toolDenialKind' AS denial
      FROM event e
     WHERE e.session_id = %s AND {_MAIN} AND e.event_type = ANY (%s)
     ORDER BY e.raw_object_key, e.raw_line_number
     LIMIT %s OFFSET %s
"""

_CONVERSATION_COUNT = f"""
    SELECT count(*) FROM event e WHERE e.session_id = %s AND {_MAIN} AND e.event_type = ANY (%s)
"""

_SESSIONS = f"""
    SELECT cs.seq, cs.start_source, cs.started_at, cs.ended_at, cs.end_reason, cs.start_commit,
           cs.end_commit, cs.run_key IS NOT NULL AS recorded,
           (SELECT count(*) FROM event e
             WHERE e.session_id = cs.session_id AND e.event_type = 'prompt' AND e.actor = 'human'
               AND NOT e.is_abandoned AND {_MAIN}
               AND e.occurred_at >= COALESCE(cs.started_at, '-infinity')
               AND (later.started_at IS NULL OR e.occurred_at < later.started_at)) AS prompts
      FROM chat_session cs
      LEFT JOIN LATERAL (SELECT min(n.started_at) AS started_at FROM chat_session n
                          WHERE n.session_id = cs.session_id AND n.seq > cs.seq) later ON TRUE
     WHERE cs.session_id = %s
     ORDER BY cs.seq
"""

_DECISIONS = """
    SELECT decision_id, seq, title, goal, options, chosen, reason, result, tests_summary,
           commit_shas, evidence_lines, status, reviewed_by_kind, reviewer, review_note,
           review_history, model, prompt_version, created_at, updated_at
      FROM decision WHERE session_id = %s ORDER BY seq
"""

_TEST_RUNS = """
    SELECT t.test_run_id, e.occurred_at, t.framework, left(t.command, 500) AS command, t.passed,
           t.exit_code, t.tests_passed, t.tests_failed, t.tests_skipped, t.output_source,
           e.is_abandoned
      FROM test_run t JOIN event e ON e.event_id = t.call_event_id
     WHERE t.session_id = %s
     ORDER BY e.occurred_at NULLS LAST, t.test_run_id
"""

_TEST_CASES = """
    SELECT c.test_run_id, c.position, c.name, c.outcome, c.message
      FROM test_case c JOIN test_run t USING (test_run_id)
     WHERE t.session_id = %s
     ORDER BY c.test_run_id, c.position
"""

_FILE_CHANGES = """
    SELECT fc.file_change_id, e.occurred_at, fc.path, fc.file_role, fc.change_type,
           fc.actor_class, fc.tool, fc.lines_added, fc.lines_removed, fc.tests_added,
           fc.user_modified, e.is_abandoned, split_part(e.raw_object_key, '/', 3) AS stream
      FROM file_change fc JOIN event e ON e.event_id = fc.event_id
     WHERE fc.session_id = %s
     ORDER BY e.occurred_at NULLS LAST, fc.file_change_id
"""

_COMMITS = """
    SELECT commit_sha, method, confidence, matched_lines, total_lines, committed_at
      FROM commit_link WHERE session_id = %s ORDER BY committed_at NULLS LAST, commit_sha
"""

_CORRECTIONS = """
    SELECT k.correction_id, k.kind, k.corrected_by, k.detected_by, k.note,
           t.occurred_at AS trigger_at, t.event_type AS trigger_type,
           f.occurred_at AS fix_at, cs.seq AS session_seq
      FROM correction k
      JOIN event t ON t.event_id = k.trigger_event_id
      LEFT JOIN event f ON f.event_id = k.fix_event_id
      LEFT JOIN chat_session cs ON cs.chat_session_id = k.chat_session_id
     WHERE k.session_id = %s
     ORDER BY t.occurred_at NULLS LAST, k.correction_id
"""

_AGENTS = """
    SELECT agent_id, agent_type, description, model, effort, spawn_depth, workflow_run_id,
           workflow_name, started_at, ended_at, event_count, left(result, 3000) AS result
      FROM agent_run WHERE session_id = %s
     ORDER BY started_at NULLS LAST, agent_id
"""

_WORKFLOWS = """
    SELECT workflow_run_id, workflow_name, status, agent_count, started_at, ended_at, event_count
      FROM v_workflow_run WHERE session_id = %s
     ORDER BY started_at NULLS LAST, workflow_run_id
"""

_THINKING = """
    SELECT e.occurred_at, split_part(e.raw_object_key, '/', 3) AS stream, t.block_index, t.text,
           t.is_redacted, e.is_abandoned
      FROM thinking t JOIN event e ON e.event_id = t.event_id
     WHERE e.session_id = %s
     ORDER BY split_part(e.raw_object_key, '/', 3) <> 'main', e.raw_object_key,
              e.raw_line_number, t.block_index
     LIMIT 2000
"""

_USAGE = """
    SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           total_tokens, cost_usd, token_source
      FROM d_usage WHERE session_id = %s ORDER BY total_tokens DESC, model
"""

_EFFORT = """
    SELECT effort, requested_model, model, count(*) AS replies,
           COALESCE(sum(output_tokens), 0)::bigint AS output_tokens,
           sum(thinking_ms)::bigint AS thinking_ms
      FROM model_usage WHERE session_id = %s
     GROUP BY effort, requested_model, model
     ORDER BY count(*) DESC, model
"""

_COST = """
    SELECT total_cost_usd, has_unknown_model_cost, lines_added, lines_removed, api_duration_ms,
           tool_duration_ms
      FROM session_cost WHERE session_id = %s
"""

_SLASH_COMMANDS = """
    SELECT occurred_at, name, arguments, is_abandoned
      FROM v_slash_command WHERE session_id = %s ORDER BY occurred_at NULLS LAST, event_id
"""

_ACCESS_LOG = """
    SELECT l.log_id, l.at, u.email, u.role, l.action, l.target
      FROM ui_access_log l LEFT JOIN ui_user u USING (user_id)
     WHERE l.log_id < %s
     ORDER BY l.log_id DESC
     LIMIT %s
"""

_ACCOUNTS = """
    SELECT account_id, account_uuid, label, email, display_name, full_name
      FROM claude_account ORDER BY label NULLS LAST, lower(email) NULLS LAST, account_id
"""

_PROJECTS = """
    SELECT project_id, project_key, label, name, key_kind FROM project
     ORDER BY label NULLS LAST, name, project_id
"""

_FIND_DECISION = "SELECT 1 FROM decision WHERE decision_id = %s AND session_id = %s"

# The same as ccp.decisions.set_decision_status: used only if that package is not installed.
_SET_DECISION_STATUS = """
    UPDATE decision
       SET status = %s, reviewed_by_kind = 'human', reviewer = %s, review_note = %s,
           updated_at = now(),
           review_history = review_history || jsonb_build_array(jsonb_build_object(
               'at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
               'by_kind', 'human', 'reviewer', %s::text, 'status', %s::text,
               'note', %s::text))
     WHERE decision_id = %s
"""


def home_tree(connection: pg8000.dbapi.Connection) -> list[Row]:
    """One row per (Claude account, project) with chat counts, tokens and last activity."""
    return _rows(connection, _HOME)


def chat_list(
    connection: pg8000.dbapi.Connection,
    *,
    project_id: int | None,
    account_id: int | None = None,
    filter_account: bool = False,
    show_empty: bool = False,
) -> list[Row]:
    """The chats of one project (``None``: chats with no project), newest first.

    Args:
        connection: An open connection.
        project_id: The project, or ``None`` for chats not linked to a project.
        account_id: With ``filter_account``: only this account's chats (``None``: no account).
        filter_account: Whether to filter by account at all.
        show_empty: Include chats that were opened and closed without a message.
    """
    where = ["c.project_id = %s" if project_id is not None else "c.project_id IS NULL"]
    parameters: list[Any] = [] if project_id is None else [project_id]
    if filter_account:
        where.append("c.account_id = %s" if account_id is not None else "c.account_id IS NULL")
        parameters += [] if account_id is None else [account_id]
    if not show_empty:
        where.append("NOT c.is_empty")
    return _rows(connection, _CHAT_LIST.format(where=" AND ".join(where)), parameters)


def chat_summary(connection: pg8000.dbapi.Connection, chat_id: str) -> Row | None:
    """One chat with its account, project and counts, or ``None`` if there is no such chat."""
    rows = _rows(connection, _CHAT_LIST.format(where="c.chat_id = %s"), [chat_id])
    return rows[0] if rows else None


def project_name(connection: pg8000.dbapi.Connection, project_id: int) -> Row | None:
    """A project's label, name and kind, or ``None``."""
    rows = _rows(
        connection,
        "SELECT project_id, label, name, key_kind, folders[1] AS folder FROM project "
        "WHERE project_id = %s",
        [project_id],
    )
    return rows[0] if rows else None


def conversation(
    connection: pg8000.dbapi.Connection, chat_id: str, page: int
) -> tuple[list[Row], int]:
    """One page of the main conversation, in transcript order, and the number of entries in all.

    Tool results carry only whether they failed or were refused, not their (large) content.
    """
    types = list(CONVERSATION_TYPES)
    total = _rows(connection, _CONVERSATION_COUNT, [chat_id, types])[0]["count"]
    offset = max(page - 1, 0) * CONVERSATION_PAGE_SIZE
    rows = _rows(connection, _CONVERSATION, [chat_id, types, CONVERSATION_PAGE_SIZE, offset])
    return rows, int(total)


def sessions(connection: pg8000.dbapi.Connection, chat_id: str) -> list[Row]:
    """The sessions of a chat (each time it was opened), with the prompts typed in each."""
    return _rows(connection, _SESSIONS, [chat_id])


def decisions(connection: pg8000.dbapi.Connection, chat_id: str) -> list[Row]:
    """The decisions of a chat, in order."""
    return _rows(connection, _DECISIONS, [chat_id])


def test_runs(connection: pg8000.dbapi.Connection, chat_id: str) -> tuple[list[Row], list[Row]]:
    """The test runs of a chat and all their test cases."""
    return _rows(connection, _TEST_RUNS, [chat_id]), _rows(connection, _TEST_CASES, [chat_id])


def file_changes(connection: pg8000.dbapi.Connection, chat_id: str) -> tuple[list[Row], list[Row]]:
    """The file changes of a chat and the commits linked to it."""
    return _rows(connection, _FILE_CHANGES, [chat_id]), _rows(connection, _COMMITS, [chat_id])


def corrections(connection: pg8000.dbapi.Connection, chat_id: str) -> list[Row]:
    """The corrections found in a chat."""
    return _rows(connection, _CORRECTIONS, [chat_id])


def agents(connection: pg8000.dbapi.Connection, chat_id: str) -> tuple[list[Row], list[Row]]:
    """The sub-agents of a chat and its Workflow runs."""
    return _rows(connection, _AGENTS, [chat_id]), _rows(connection, _WORKFLOWS, [chat_id])


def thinking(connection: pg8000.dbapi.Connection, chat_id: str) -> list[Row]:
    """The thinking blocks of a chat (main conversation first, then sub-agents)."""
    return _rows(connection, _THINKING, [chat_id])


def usage(connection: pg8000.dbapi.Connection, chat_id: str) -> dict[str, list[Row]]:
    """Tokens by model, replies by effort, the cost record and the slash commands of a chat."""
    return {
        "models": _rows(connection, _USAGE, [chat_id]),
        "effort": _rows(connection, _EFFORT, [chat_id]),
        "cost": _rows(connection, _COST, [chat_id]),
        "commands": _rows(connection, _SLASH_COMMANDS, [chat_id]),
    }


def access_log(
    connection: pg8000.dbapi.Connection, before: int | None, limit: int = 200
) -> list[Row]:
    """The newest access-log rows (before the row id ``before``, for the next page)."""
    return _rows(connection, _ACCESS_LOG, [before if before is not None else 2**62, limit])


def accounts(connection: pg8000.dbapi.Connection) -> list[Row]:
    """Every Claude account (for linking users to accounts)."""
    return _rows(connection, _ACCOUNTS)


def projects(connection: pg8000.dbapi.Connection) -> list[Row]:
    """Every project (for the label page)."""
    return _rows(connection, _PROJECTS)


def review_decision(
    connection: pg8000.dbapi.Connection,
    chat_id: str,
    decision_id: int,
    status: str,
    reviewer: str,
    note: str,
) -> bool:
    """Record a person's review of a decision of this chat.

    Uses ``set_decision_status`` of :mod:`ccp.decisions` when that package is installed, so that
    the rules stay in one place; otherwise makes the same change here: the status, reviewed by a
    human, the reviewer and note, and one more entry in ``review_history``.

    Returns:
        False if the chat has no decision with that id (nothing is changed).
    """
    if not _rows(connection, _FIND_DECISION, [decision_id, chat_id]):
        return False
    setter = decision_status_setter()
    if setter is not None:
        setter(connection, decision_id, status, reviewer, note)
    else:
        connection.cursor().execute(
            _SET_DECISION_STATUS,
            (status, reviewer, note or None, reviewer, status, note or None, decision_id),
        )
    return True


def decision_status_setter() -> Callable[..., Any] | None:
    """``set_decision_status`` from the decisions package, or ``None`` if it is not there.

    Imported only when needed: the decisions package is optional for the website.
    """
    for module_name in ("ccp.decisions", "ccp.decisions.review", "ccp.decisions.store"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001, S112  (a package still being written must not break the site)
            continue
        setter = getattr(module, "set_decision_status", None)
        if callable(setter):
            return setter  # type: ignore[no-any-return]
    return None


def as_json_list(value: Any) -> list[Any]:
    """A JSON list column as a Python list (pg8000 may return JSON text for some types)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _rows(
    connection: pg8000.dbapi.Connection, sql: str, parameters: list[Any] | None = None
) -> list[Row]:
    """Run a query and return its rows as dictionaries keyed by column name."""
    cursor = connection.cursor()
    if parameters:
        cursor.execute(sql, tuple(parameters))
    else:
        cursor.execute(sql)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
