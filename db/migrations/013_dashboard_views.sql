-- 013_dashboard_views: views for dashboards (Step 3, task 3.3).
--
-- A dashboard shows counts, costs and trends, never what was said. These views expose exactly that and
-- nothing else: no prompts, no replies, no file contents, no commands, no names of people or machines
-- (the environment record is reduced to the surface, versions and operating system). A dashboard tool
-- should connect with a role that may read these views only (infra/metabase/readonly-role.sql), so that
-- it cannot reach the tables behind them.
--
-- Prefix `d_` marks a dashboard view. The audit views of migration 011 (`v_...`) show content and are for
-- people with access to the audit trail.

-- One row per session, with its size and outcome.
CREATE VIEW d_session AS
SELECT s.session_id,
       s.started_at,
       s.started_at::date AS day,
       s.branch,
       s.status,
       COALESCE(env.surface, 'unknown') AS surface,
       env.entrypoint,
       env.claude_code_version,
       env.os_name,
       env.run_mode,
       env.auth_type,
       c.total_cost_usd,
       c.has_unknown_model_cost,
       (SELECT count(*) FROM event e
         WHERE e.session_id = s.session_id AND e.event_type = 'prompt' AND e.actor = 'human'
           AND NOT e.is_abandoned AND split_part(e.raw_object_key, '/', 3) = 'main') AS prompts,
       (SELECT count(*) FROM file_change f JOIN event e ON e.event_id = f.event_id
         WHERE f.session_id = s.session_id AND NOT e.is_abandoned) AS file_changes,
       (SELECT COALESCE(sum(f.lines_added), 0) FROM file_change f JOIN event e ON e.event_id = f.event_id
         WHERE f.session_id = s.session_id AND NOT e.is_abandoned) AS lines_added,
       (SELECT COALESCE(sum(f.lines_removed), 0) FROM file_change f JOIN event e ON e.event_id = f.event_id
         WHERE f.session_id = s.session_id AND NOT e.is_abandoned) AS lines_removed,
       (SELECT count(*) FROM test_run t WHERE t.session_id = s.session_id) AS test_runs,
       (SELECT count(*) FROM test_run t WHERE t.session_id = s.session_id AND t.passed = FALSE)
           AS test_failures,
       (SELECT count(*) FROM agent_run a WHERE a.session_id = s.session_id) AS agent_runs,
       (SELECT count(*) FROM compaction k WHERE k.session_id = s.session_id) AS compactions,
       (SELECT count(*) FROM v_human_decision d
         WHERE d.session_id = s.session_id AND d.decision_type = 'rewind') AS rewinds,
       (SELECT count(*) FROM v_human_decision d
         WHERE d.session_id = s.session_id AND d.decision_type = 'denial') AS denials,
       (SELECT count(*) FROM commit_link l WHERE l.session_id = s.session_id) AS commits
  FROM session s
  LEFT JOIN session_env env USING (session_id)
  LEFT JOIN session_cost c USING (session_id);

-- Cost and tokens per session and model (helper models included).
CREATE VIEW d_model_cost AS
SELECT m.session_id, s.started_at::date AS day, m.model, m.input_tokens, m.output_tokens,
       m.thinking_tokens, m.cache_read_tokens, m.cache_creation_tokens, m.cost_usd
  FROM session_model_cost m
  JOIN session s USING (session_id);

-- One row per AI edit of a file: how much, by which tool, and by whom (actor class).
CREATE VIEW d_edit AS
SELECT f.session_id, e.occurred_at, e.occurred_at::date AS day, f.path, f.tool, f.change_type,
       f.actor_class, f.user_modified, f.lines_added, f.lines_removed, e.is_abandoned
  FROM file_change f
  JOIN event e ON e.event_id = f.event_id;

-- One row per test run: did it pass?
CREATE VIEW d_test_run AS
SELECT t.session_id, e.occurred_at, e.occurred_at::date AS day, t.framework, t.passed, t.exit_code
  FROM test_run t
  JOIN event e ON e.event_id = t.call_event_id;

-- Fix loops: a failed run followed by a pass, with the edits in between and how long it took.
CREATE VIEW d_fix_loop AS
SELECT c.session_id, c.framework, c.failed_at, c.passed_at, c.edits_between,
       round((extract(epoch FROM (c.passed_at - c.failed_at)) / 60)::numeric, 1) AS minutes_to_fix
  FROM v_test_fix_cycle c;
