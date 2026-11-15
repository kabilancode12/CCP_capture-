-- Undo 016_chat_hierarchy: back to the state of migration 015.
--
-- The labels go back into account_label and project_label. Everything else that 016 added is dropped,
-- including the decisions, chat sessions and explorer users recorded since. The roles that could read
-- d_usage may read the old d_usage again.

CREATE TEMPORARY TABLE d_usage_reader ON COMMIT DROP AS
SELECT DISTINCT acl.grantee
  FROM pg_class c
  CROSS JOIN LATERAL aclexplode(c.relacl) acl
 WHERE c.oid = 'd_usage'::regclass AND acl.privilege_type = 'SELECT'
   AND acl.grantee <> c.relowner AND acl.grantee <> 0;

DROP VIEW d_chat;
DROP VIEW v_workflow_run;
DROP VIEW v_slash_command;
DROP VIEW d_usage;
DROP VIEW chat;

CREATE TABLE account_label (
    account_email TEXT PRIMARY KEY,   -- compared in lower case
    label         TEXT NOT NULL CHECK (label ~ '^[A-Za-z0-9 _.-]{1,40}$')
);

CREATE TABLE project_label (
    folder TEXT PRIMARY KEY,          -- the session's working folder, exactly as Claude Code wrote it
    label  TEXT NOT NULL CHECK (label ~ '^[A-Za-z0-9 _.-]{1,40}$')
);

INSERT INTO account_label (account_email, label)
SELECT DISTINCT ON (lower(email)) lower(email), label
  FROM claude_account
 WHERE email IS NOT NULL AND label IS NOT NULL
 ORDER BY lower(email), account_id;

INSERT INTO project_label (folder, label)
SELECT DISTINCT ON (folder) folder, label
  FROM project, unnest(folders) AS folder
 WHERE label IS NOT NULL
 ORDER BY folder, project_id;

CREATE VIEW d_usage AS
WITH from_cost_state AS (
    SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           cost_usd
      FROM session_model_cost
), from_responses AS (
    SELECT u.session_id, u.model,
           sum(u.input_tokens)::bigint          AS input_tokens,
           sum(u.output_tokens)::bigint         AS output_tokens,
           sum(u.cache_read_tokens)::bigint     AS cache_read_tokens,
           sum(u.cache_creation_tokens)::bigint AS cache_creation_tokens
      FROM model_usage u
     WHERE u.model IS NOT NULL AND u.model <> '<synthetic>'
       AND NOT EXISTS (SELECT 1 FROM session_model_cost c WHERE c.session_id = u.session_id)
     GROUP BY u.session_id, u.model
), usage AS (
    SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           cost_usd, 'cost-state' AS token_source
      FROM from_cost_state
    UNION ALL
    SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           NULL::numeric, 'responses'
      FROM from_responses
)
SELECT u.session_id,
       s.started_at::date AS day,
       COALESCE(pl.label,
                NULLIF(regexp_replace(rtrim(f.folder, '/\'), '^.*[/\\]', ''), ''),
                'unknown') AS project,
       COALESCE(al.label,
                'account-' || left(md5(lower(env.account_email)), 6),
                'unknown') AS account,
       COALESCE(env.surface, 'unknown') AS surface,
       u.model,
       COALESCE(u.input_tokens, 0)          AS input_tokens,
       COALESCE(u.output_tokens, 0)         AS output_tokens,
       COALESCE(u.cache_read_tokens, 0)     AS cache_read_tokens,
       COALESCE(u.cache_creation_tokens, 0) AS cache_creation_tokens,
       COALESCE(u.input_tokens, 0) + COALESCE(u.output_tokens, 0)
         + COALESCE(u.cache_read_tokens, 0) + COALESCE(u.cache_creation_tokens, 0) AS total_tokens,
       u.cost_usd,
       u.token_source
  FROM usage u
  JOIN session s USING (session_id)
  LEFT JOIN session_env env USING (session_id)
  LEFT JOIN v_session_folder f USING (session_id)
  LEFT JOIN project_label pl ON pl.folder = f.folder
  LEFT JOIN account_label al ON al.account_email = lower(env.account_email);

DO $$
DECLARE
    reader REGROLE;
BEGIN
    FOR reader IN SELECT grantee::regrole FROM d_usage_reader LOOP
        EXECUTE 'GRANT SELECT ON d_usage TO ' || reader::text;
    END LOOP;
END
$$;

DROP TABLE ui_access_log;
DROP TABLE ui_user;
DROP TABLE decision;
DROP TABLE correction;
DROP TABLE test_case;
DROP TABLE chat_session;

ALTER TABLE test_run
    DROP COLUMN tests_passed,
    DROP COLUMN tests_failed,
    DROP COLUMN tests_skipped,
    DROP COLUMN output_source;

ALTER TABLE file_change
    DROP COLUMN file_role,
    DROP COLUMN tests_added;

DROP INDEX agent_run_workflow_idx;
ALTER TABLE agent_run
    DROP COLUMN effort,
    DROP COLUMN workflow_run_id,
    DROP COLUMN workflow_name;

ALTER TABLE model_usage
    DROP COLUMN effort,
    DROP COLUMN requested_model,
    DROP COLUMN thinking_ms;

ALTER TABLE session_env
    DROP COLUMN account_uuid,
    DROP COLUMN full_name,
    DROP COLUMN machine_key,
    DROP COLUMN folder,
    DROP COLUMN git_remote,
    DROP COLUMN git_user_name,
    DROP COLUMN git_user_email,
    DROP COLUMN organisation_role;

ALTER TABLE session
    DROP COLUMN account_id,
    DROP COLUMN project_id,
    DROP COLUMN title,
    DROP COLUMN is_empty,
    DROP COLUMN git_user_name,
    DROP COLUMN git_user_email;

DROP TABLE project;
DROP TABLE claude_account;
