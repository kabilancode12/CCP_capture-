-- 015_usage_view: token and cost usage by project, Claude account and model, for developers.
--
-- Developers want to see how many tokens they use, in which project, with which model and with which
-- Claude account. This migration adds:
--
--   * `account_label` and `project_label`: short names an administrator gives to a Claude account
--     ("claude1") and to a project folder ("billing-api"). Optional: without a label a neutral name is
--     shown.
--   * `d_usage`: one row per session and model, with tokens and cost and the labels above.
--
-- `d_usage` is a dashboard view (prefix `d_`), so the rule of migration 013 holds: counts and short
-- categorical text only. It never shows an e-mail address, a full folder path (which holds the user's
-- name), a prompt or a reply:
--
--   * the account is its label, or `account-` plus 6 characters of a hash of its e-mail address;
--   * the project is its label, or the last part of the session's folder (`ccp_V2`).
--
-- Tokens come from two sources. Claude Code's own `cost-state` entry (table `session_model_cost`)
-- is preferred, because it is what Claude Code counted and it includes helper models. When a session
-- has no `cost-state` entry (seen on Claude Code 2.1.323), the tokens of the recorded model responses
-- (table `model_usage`) are summed instead and the cost stays empty. `token_source` says which one a
-- row uses.

CREATE TABLE account_label (
    account_email TEXT PRIMARY KEY,   -- compared in lower case
    label         TEXT NOT NULL CHECK (label ~ '^[A-Za-z0-9 _.-]{1,40}$')
);

CREATE TABLE project_label (
    folder TEXT PRIMARY KEY,          -- the session's working folder, exactly as Claude Code wrote it
    label  TEXT NOT NULL CHECK (label ~ '^[A-Za-z0-9 _.-]{1,40}$')
);

-- The folder a session started in: the `cwd` of its first main-transcript entry that has one.
CREATE VIEW v_session_folder AS
SELECT s.session_id,
       (SELECT e.payload ->> 'cwd'
          FROM event e
         WHERE e.session_id = s.session_id
           AND split_part(e.raw_object_key, '/', 3) = 'main'
           AND e.payload ? 'cwd'
         ORDER BY e.raw_object_key, e.raw_line_number
         LIMIT 1) AS folder
  FROM session s;

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
