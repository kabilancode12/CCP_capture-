-- 011_saved_questions: the six questions the audit trail has to answer, as views (Step 2, task 2.7).
--
-- Each view is read-only and built on the indexer's tables, so it needs no maintenance: it always
-- shows the current index. Filter and sort them in your own query, for example
--     SELECT * FROM v_file_history WHERE path = 'src/app.py' ORDER BY occurred_at;
-- Rewound work is KEPT and flagged (`is_abandoned`); leave it out with `WHERE NOT is_abandoned`.
-- Documented in docs/queries.md.

-- 1. Cost and models per session. Cost is read from the transcript, not computed.
CREATE VIEW v_session_cost AS
SELECT s.session_id, s.started_at, s.branch, s.status,
       c.total_cost_usd, c.has_unknown_model_cost,
       (SELECT array_agg(m.model ORDER BY m.cost_usd DESC NULLS LAST)
          FROM session_model_cost m WHERE m.session_id = s.session_id) AS models,
       (SELECT sum(m.input_tokens) FROM session_model_cost m WHERE m.session_id = s.session_id)
           AS input_tokens,
       (SELECT sum(m.output_tokens) FROM session_model_cost m WHERE m.session_id = s.session_id)
           AS output_tokens,
       (SELECT sum(m.cache_read_tokens) FROM session_model_cost m WHERE m.session_id = s.session_id)
           AS cache_read_tokens,
       (SELECT sum(m.cache_creation_tokens)
          FROM session_model_cost m WHERE m.session_id = s.session_id) AS cache_creation_tokens
  FROM session s
  LEFT JOIN session_cost c USING (session_id);

-- 2. Every change to a file, with who made it and the prompt behind it. `actor_class` is ai_edit
--    for now (see file_change). `prompt` is the start of the developer's prompt (500 characters).
CREATE VIEW v_file_history AS
SELECT f.path, f.session_id, e.occurred_at, e.is_abandoned, f.tool, f.change_type, f.actor_class,
       f.user_modified, f.lines_added, f.lines_removed, f.hunks,
       f.prompt_event_id, f.call_event_id, f.event_id,
       left(CASE WHEN jsonb_typeof(p.payload #> '{message,content}') = 'string'
                 THEN p.payload #>> '{message,content}'
                 ELSE jsonb_path_query_first(
                          p.payload, '$.message.content[*] ? (@.type == "text").text') #>> '{}'
            END, 500) AS prompt
  FROM file_change f
  JOIN event e ON e.event_id = f.event_id
  LEFT JOIN event p ON p.event_id = f.prompt_event_id;

-- 3. Sub-agents of a session: what they were told and what they returned. `parent_agent_id` is
--    NULL when the main conversation launched the agent, else the agent that did.
CREATE VIEW v_agent_run AS
SELECT a.session_id, a.agent_id, a.agent_type, a.description, a.spawn_depth, a.model,
       a.instructions, a.result, a.started_at, a.ended_at, a.event_count,
       a.launch_event_id,
       CASE WHEN starts_with(split_part(l.raw_object_key, '/', 3), 'agent-')
            THEN substr(split_part(l.raw_object_key, '/', 3), 7) END AS parent_agent_id
  FROM agent_run a
  LEFT JOIN event l ON l.event_id = a.launch_event_id;

-- 4. Decisions taken by a person: a refused tool call (and how it was refused), a rewind (the
--    prompt whose branch was abandoned; `occurred_at` is when the replacing prompt was typed),
--    and a plan approval or rejection (ExitPlanMode; this shape has not been seen in a real
--    transcript yet, so treat it as unverified).
CREATE VIEW v_human_decision AS
SELECT r.session_id, 'denial' AS decision_type, r.event_id, r.occurred_at,
       r.payload ->> 'toolDenialKind' AS detail,
       jsonb_path_query_first(c.payload, '$.message.content[*] ? (@.type == "tool_use").name')
           #>> '{}' AS tool
  FROM event r
  LEFT JOIN event c ON c.session_id = r.session_id AND c.event_type = 'tool_call'
                   AND c.tool_use_ids && r.tool_use_ids
 WHERE r.event_type = 'tool_result' AND r.payload ? 'toolDenialKind'
UNION ALL
SELECT e.session_id, 'rewind', e.event_id,
       (SELECT min(s.occurred_at) FROM event s
         WHERE s.session_id = e.session_id AND s.parent_uuid = e.parent_uuid
           AND s.event_type = 'prompt' AND s.actor = 'human' AND NOT s.is_abandoned),
       'abandoned prompt', NULL
  FROM event e
 WHERE e.event_type = 'prompt' AND e.actor = 'human' AND e.is_abandoned
   AND NOT EXISTS (SELECT 1 FROM event p
                    WHERE p.session_id = e.session_id AND p.entry_uuid = e.parent_uuid
                      AND p.is_abandoned)
UNION ALL
SELECT r.session_id, 'plan_decision', r.event_id, r.occurred_at,
       CASE WHEN jsonb_path_exists(
                     r.payload,
                     '$.message.content[*] ? (@.type == "tool_result" && @.is_error == true)')
            THEN 'rejected' ELSE 'approved' END,
       'ExitPlanMode'
  FROM event r
  JOIN event c ON c.session_id = r.session_id AND c.event_type = 'tool_call'
              AND c.tool_use_ids && r.tool_use_ids
 WHERE r.event_type = 'tool_result'
   AND jsonb_path_exists(c.payload, '$.message.content[*] ? (@.type == "tool_use" && @.name == "ExitPlanMode")');

-- 5. Where a session compacted: when, how large the conversation was before and after, and how
--    many prompts the developer had typed by then (counting kept work only).
CREATE VIEW v_compaction AS
SELECT c.session_id, e.occurred_at, c.trigger, c.pre_tokens, c.post_tokens,
       (SELECT count(*) FROM event p
         WHERE p.session_id = c.session_id AND p.event_type = 'prompt' AND p.actor = 'human'
           AND NOT p.is_abandoned AND split_part(p.raw_object_key, '/', 3) = 'main'
           AND (p.raw_object_key, p.raw_line_number) < (e.raw_object_key, e.raw_line_number))
           AS prompts_before,
       c.summary, c.event_id
  FROM compaction c
  JOIN event e ON e.event_id = c.event_id;

-- 6. Where tests failed and then passed after edits: for each failed run, the next passed run of
--    the same runner in the same session, and how many file changes happened in between. Time
--    decides the order (entries without a time are not matched).
CREATE VIEW v_test_fix_cycle AS
SELECT f.session_id, f.framework, f.command AS failed_command,
       ef.occurred_at AS failed_at, p.passed_run_id, p.passed_at,
       (SELECT count(*) FROM file_change fc JOIN event e ON e.event_id = fc.event_id
         WHERE fc.session_id = f.session_id AND NOT e.is_abandoned
           AND e.occurred_at > ef.occurred_at AND e.occurred_at < p.passed_at) AS edits_between,
       f.test_run_id AS failed_run_id
  FROM test_run f
  JOIN event ef ON ef.event_id = f.call_event_id
  JOIN LATERAL (
      SELECT t.test_run_id AS passed_run_id, et.occurred_at AS passed_at
        FROM test_run t JOIN event et ON et.event_id = t.call_event_id
       WHERE t.session_id = f.session_id AND t.framework = f.framework AND t.passed
         AND et.occurred_at > ef.occurred_at
       ORDER BY et.occurred_at LIMIT 1) p ON TRUE
 WHERE f.passed = FALSE;
