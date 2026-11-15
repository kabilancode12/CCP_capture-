-- 016_chat_hierarchy: organise every captured chat as Claude account -> project -> chat -> parts.
--
-- Names used from here on (they replace the looser "session" wording in new code and docs):
--
--   * Claude account: one person's Claude login (table `claude_account`).
--   * Project: one Git repository, keyed by its normalised `origin` remote (for example
--     `github.com/acme/billing`); a folder without a remote is its own project, keyed by
--     `<host name>:<folder>` (table `project`).
--   * Chat: one Claude Code conversation: one id, one transcript. The existing table `session` holds the
--     chats and keeps its name (much code depends on it); the view `chat` shows it with friendly names.
--   * Session: each time a chat is opened (start, resume, after /clear or /compact), from the
--     SessionStart hook to the SessionEnd hook or the idle timeout (table `chat_session`).
--   * Decision, test case, correction: parts of a chat (tables `decision`, `test_case`, `correction`).
--
-- The whole schema of this stage is created here, so that the indexer, the catalog and the explorer
-- website need no further schema change. Tables that later stages fill stay empty until then.
--
-- The labels of migration 015 move into the new tables: `account_label` becomes `claude_account.label`
-- (matched by lower-case e-mail address) and `project_label` becomes `project.label` (matched by
-- folder). Both old tables are dropped. `d_usage` keeps its columns and reads the new tables.
--
-- Jobs: `job.job_type` has no CHECK constraint (migration 001), so the new job types `distil_chat` and
-- `catalog_chat` need no change here.
--
-- Read-only roles: `d_usage` is dropped and created again, which would remove the SELECT right that
-- `ccp-db create-reader` gave it. The roles that could read it keep that right on the new `d_usage`
-- and also get the new `d_chat`. Run `ccp-db create-reader` and `ccp-db create-auditor` again after
-- this migration all the same, so that the roles match the current lists exactly.
--
-- Folders: a Windows folder is keyed with a lower-case drive letter and backslashes (`c:\work\app`),
-- because Claude Code writes `C:\work\app` in one place and `c:\work\app` in another. Other folders
-- are keyed exactly as written. ccp.db.hierarchy.normalise_folder applies the same rule.

-- ------------------------------------------------------------------ Claude accounts and projects

CREATE TABLE claude_account (
    account_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_uuid      TEXT UNIQUE,              -- oauthAccount.accountUuid; NULL for rows known only by e-mail
    email             TEXT,
    full_name         TEXT,
    display_name      TEXT,
    organisation_uuid TEXT,
    organisation_name TEXT,
    organisation_role TEXT,
    label             TEXT CHECK (label ~ '^[A-Za-z0-9 _.-]{1,40}$'),  -- the name shown on dashboards
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Before account uuids were collected, an account was known by its e-mail address only: one such row
-- per address. When the uuid arrives, that row takes it over (see ccp.db.hierarchy).
CREATE UNIQUE INDEX claude_account_email_only_idx
    ON claude_account (lower(email)) WHERE account_uuid IS NULL;

CREATE TABLE project (
    project_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_key   TEXT NOT NULL UNIQUE,         -- normalised Git remote, or <host>:<folder>
    key_kind      TEXT NOT NULL CHECK (key_kind IN ('git_remote', 'folder')),
    name          TEXT NOT NULL,                -- last part of the remote or folder (billing, ccp_V2)
    git_remote    TEXT,                         -- the remote as reported, without credentials
    folders       TEXT[] NOT NULL DEFAULT '{}', -- every folder a chat of this project ran in
    label         TEXT CHECK (label ~ '^[A-Za-z0-9 _.-]{1,40}$'),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX project_folders_idx ON project USING GIN (folders);

-- ------------------------------------------------------------------ chats (table session)

ALTER TABLE session
    ADD COLUMN account_id     BIGINT REFERENCES claude_account ON DELETE SET NULL,
    ADD COLUMN project_id     BIGINT REFERENCES project ON DELETE SET NULL,
    ADD COLUMN title          TEXT,             -- the latest ai-title entry of the transcript
    ADD COLUMN is_empty       BOOLEAN NOT NULL DEFAULT FALSE,  -- opened and closed without a message
    ADD COLUMN git_user_name  TEXT,
    ADD COLUMN git_user_email TEXT;
CREATE INDEX session_account_idx ON session (account_id);
CREATE INDEX session_project_idx ON session (project_id);

ALTER TABLE session_env
    ADD COLUMN account_uuid      TEXT,
    ADD COLUMN full_name         TEXT,
    ADD COLUMN machine_key       TEXT,          -- first 16 hex of SHA-256 of Claude Code's machineID
    ADD COLUMN folder            TEXT,          -- the hook's working folder (cwd)
    ADD COLUMN git_remote        TEXT,          -- `git remote get-url origin`, credentials removed
    ADD COLUMN git_user_name     TEXT,
    ADD COLUMN git_user_email    TEXT,
    ADD COLUMN organisation_role TEXT;

-- Each time a chat is opened. `run_key` is made by the capture client at SessionStart and sent again
-- at SessionEnd, so uploads are idempotent; rows the indexer infers from old transcripts have none.
CREATE TABLE chat_session (
    chat_session_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    seq             INTEGER NOT NULL,           -- 1, 2, 3 ... within the chat
    start_source    TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (start_source IN ('startup', 'resume', 'clear', 'compact', 'inferred', 'unknown')),
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    end_reason      TEXT CHECK (end_reason IN ('clear', 'resume', 'logout', 'prompt_input_exit', 'other',
                                               'idle_timeout', 'inferred')),
    start_commit    TEXT,
    end_commit      TEXT,
    run_key         TEXT UNIQUE,
    UNIQUE (session_id, seq)
);

-- ------------------------------------------------------------------ new facts on indexed tables

ALTER TABLE model_usage
    ADD COLUMN effort          TEXT,            -- low | medium | high | xhigh | max
    ADD COLUMN requested_model TEXT,
    ADD COLUMN thinking_ms     BIGINT;

ALTER TABLE agent_run
    ADD COLUMN effort          TEXT,
    ADD COLUMN workflow_run_id TEXT,            -- set for sub-agents started by the Workflow tool
    ADD COLUMN workflow_name   TEXT;
CREATE INDEX agent_run_workflow_idx ON agent_run (session_id, workflow_run_id);

ALTER TABLE file_change
    ADD COLUMN file_role   TEXT CHECK (file_role IN ('test', 'code', 'docs', 'config', 'other')),
    ADD COLUMN tests_added INTEGER;

ALTER TABLE test_run
    ADD COLUMN tests_passed  INTEGER,
    ADD COLUMN tests_failed  INTEGER,
    ADD COLUMN tests_skipped INTEGER,
    ADD COLUMN output_source TEXT CHECK (output_source IN ('inline', 'tool_result_file', 'missing'));

-- One test inside a test run (pytest -> test_login failed).
CREATE TABLE test_case (
    test_case_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    test_run_id  BIGINT NOT NULL REFERENCES test_run ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('passed', 'failed', 'error', 'skipped')),
    message      TEXT,                          -- short and redacted
    UNIQUE (test_run_id, position)
);

-- Something that had to be put right: a failing test fixed, a command retried, work redone, a rewind,
-- a refused action, or the developer correcting Claude. Index data: rebuilt from the events.
CREATE TABLE correction (
    correction_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    chat_session_id  BIGINT REFERENCES chat_session ON DELETE SET NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('test_fix', 'command_retry', 'rework', 'rewind',
                                                   'refused', 'developer_correction')),
    corrected_by     TEXT NOT NULL CHECK (corrected_by IN ('claude', 'test', 'developer')),
    trigger_event_id BIGINT NOT NULL REFERENCES event ON DELETE CASCADE,
    fix_event_id     BIGINT REFERENCES event ON DELETE SET NULL,
    detected_by      TEXT NOT NULL CHECK (detected_by IN ('rule', 'ai')),
    note             TEXT,
    UNIQUE (session_id, kind, trigger_event_id)
);
CREATE INDEX correction_session_idx ON correction (session_id);

-- One task's goal, options, choice and reason inside a chat. NOT index data: written by the
-- distillation and reviewed by people or by an AI reviewer, so a rebuild of the index never deletes it.
CREATE TABLE decision (
    decision_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    title              TEXT,
    goal               TEXT,
    options            JSONB,
    chosen             TEXT,
    reason             TEXT,
    result             TEXT,
    tests_summary      TEXT,
    commit_shas        TEXT[],
    -- Where the evidence is, as [{"key": "<raw object key>", "line": <line number>}, ...]: the raw
    -- transcript lines, not event ids, because a rebuild of the index numbers the events anew while
    -- decisions are kept.
    evidence_lines     JSONB NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'approved', 'rejected', 'needs_review')),
    reviewed_by_kind   TEXT CHECK (reviewed_by_kind IN ('ai', 'human')),
    reviewer           TEXT,
    review_note        TEXT,
    review_history     JSONB NOT NULL DEFAULT '[]',
    model              TEXT,
    prompt_version     TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, seq)
);

-- ------------------------------------------------------------------ the explorer website's users

CREATE TABLE ui_user (
    user_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    full_name     TEXT,
    role          TEXT NOT NULL CHECK (role IN ('developer', 'lead', 'auditor', 'admin')),
    account_id    BIGINT REFERENCES claude_account ON DELETE SET NULL,
    password_hash TEXT,
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ui_access_log (
    log_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT REFERENCES ui_user ON DELETE SET NULL,
    action  TEXT NOT NULL,
    target  TEXT,
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ui_access_log_user_idx ON ui_access_log (user_id, at);

-- ------------------------------------------------------------------ fill from what exists

-- The folder of chats captured so far (the old client did not send it).
UPDATE session_env env SET folder = f.folder
  FROM v_session_folder f
 WHERE f.session_id = env.session_id AND env.folder IS NULL AND f.folder IS NOT NULL;

-- Claude accounts seen so far, known by e-mail address (no uuid was collected), with their labels.
INSERT INTO claude_account (email, display_name, organisation_uuid, organisation_name, label,
                            first_seen_at, last_seen_at)
SELECT COALESCE(seen.email, l.account_email), seen.display_name, seen.organisation_uuid,
       seen.organisation_name, l.label,
       COALESCE(seen.first_seen_at, now()), COALESCE(seen.last_seen_at, now())
  FROM (SELECT lower(env.account_email) AS email,
               max(env.account_name) AS display_name,
               max(env.organisation_uuid) AS organisation_uuid,
               max(env.organisation_name) AS organisation_name,
               min(COALESCE(s.started_at, s.last_activity_at)) AS first_seen_at,
               max(s.last_activity_at) AS last_seen_at
          FROM session_env env
          JOIN session s USING (session_id)
         WHERE env.account_email IS NOT NULL
         GROUP BY lower(env.account_email)) seen
  FULL JOIN account_label l ON l.account_email = seen.email;

UPDATE session s SET account_id = a.account_id
  FROM session_env env, claude_account a
 WHERE env.session_id = s.session_id AND lower(a.email) = lower(env.account_email);

-- Projects seen so far: each folder is its own project (no Git remote was collected), keyed by
-- <host name>:<folder> as ccp.db.hierarchy does, or by the folder alone when the host is unknown.
-- The key of each chat is worked out once, in a table that is gone at the end of this migration.
CREATE TEMPORARY TABLE chat_project_key ON COMMIT DROP AS
SELECT s.session_id, env.folder, s.started_at, s.last_activity_at,
       COALESCE(lower(NULLIF(btrim(env.hostname), '')) || ':', '') || k.folder_key AS project_key
  FROM session_env env
  JOIN session s USING (session_id)
  CROSS JOIN LATERAL (
      SELECT CASE WHEN env.folder ~ '^[A-Za-z]:[\\/]'
                  THEN lower(left(env.folder, 1)) || replace(substr(env.folder, 2), '/', '\')
                  ELSE env.folder END AS folder_key) k
 WHERE env.folder IS NOT NULL;

INSERT INTO project (project_key, key_kind, name, folders, first_seen_at, last_seen_at)
SELECT project_key, 'folder',
       COALESCE(NULLIF(regexp_replace(rtrim(min(folder), '/\'), '^.*[/\\]', ''), ''), min(folder)),
       array_agg(DISTINCT folder ORDER BY folder),
       min(COALESCE(started_at, last_activity_at)), max(last_activity_at)
  FROM chat_project_key
 GROUP BY project_key;

UPDATE project p SET label = l.label FROM project_label l WHERE l.folder = ANY (p.folders);

-- A label for a folder no chat has used yet: the project is keyed by the folder alone, and the first
-- chat in that folder takes it over with its label (ccp.db.hierarchy.link_chat).
INSERT INTO project (project_key, key_kind, name, folders, label)
SELECT CASE WHEN l.folder ~ '^[A-Za-z]:[\\/]'
            THEN lower(left(l.folder, 1)) || replace(substr(l.folder, 2), '/', '\')
            ELSE l.folder END,
       'folder',
       COALESCE(NULLIF(regexp_replace(rtrim(l.folder, '/\'), '^.*[/\\]', ''), ''), l.folder),
       ARRAY[l.folder], l.label
  FROM project_label l
 WHERE NOT EXISTS (SELECT 1 FROM project p WHERE l.folder = ANY (p.folders))
ON CONFLICT (project_key) DO NOTHING;

UPDATE session s SET project_id = p.project_id
  FROM chat_project_key k, project p
 WHERE k.session_id = s.session_id AND p.project_key = k.project_key;

-- Who could read d_usage before it is dropped (the reader and auditor roles, under any name), so that
-- they can read the new d_usage and d_chat too (see the top of this file).
CREATE TEMPORARY TABLE d_usage_reader ON COMMIT DROP AS
SELECT DISTINCT acl.grantee
  FROM pg_class c
  CROSS JOIN LATERAL aclexplode(c.relacl) acl
 WHERE c.oid = 'd_usage'::regclass AND acl.privilege_type = 'SELECT'
   AND acl.grantee <> c.relowner AND acl.grantee <> 0;

-- ------------------------------------------------------------------ views

DROP VIEW d_usage;
DROP TABLE account_label;
DROP TABLE project_label;

-- A chat with friendly column names, its Claude account and its project. For audit use (it shows
-- e-mail addresses and folders): dashboards use d_chat. A chat not yet linked to an account or project
-- (an older capture client, not indexed yet) is matched by its account uuid or e-mail and its folder.
CREATE VIEW chat AS
SELECT s.session_id AS chat_id,
       s.title,
       a.account_id,
       COALESCE(a.display_name, a.full_name, env.account_name) AS account_name,
       COALESCE(a.email, env.account_email) AS account_email,
       a.label AS account_label,
       p.project_id,
       p.name AS project_name,
       p.label AS project_label,
       p.key_kind AS project_kind,
       owner.folder,
       s.branch,
       s.start_commit,
       s.started_at,
       s.ended_at,
       s.last_activity_at,
       s.status,
       s.is_empty,
       s.start_source,
       s.end_reason,
       s.git_user_name,
       s.git_user_email,
       env.surface,
       (SELECT count(*) FROM chat_session cs WHERE cs.session_id = s.session_id) AS session_count
  FROM session s
  LEFT JOIN session_env env USING (session_id)
  CROSS JOIN LATERAL (
      SELECT COALESCE(env.folder,
                      (SELECT f.folder FROM v_session_folder f WHERE f.session_id = s.session_id))
                 AS folder) folder_of
  CROSS JOIN LATERAL (
      SELECT folder_of.folder,
             COALESCE(s.account_id,
                      (SELECT c.account_id FROM claude_account c
                        WHERE c.account_uuid = env.account_uuid),
                      (SELECT c.account_id FROM claude_account c
                        WHERE lower(c.email) = lower(env.account_email)
                        ORDER BY c.account_uuid IS NULL, c.account_id LIMIT 1)) AS account_id,
             COALESCE(s.project_id,
                      (SELECT q.project_id FROM project q
                        WHERE folder_of.folder = ANY (q.folders)
                        ORDER BY q.key_kind = 'git_remote' DESC, q.project_id LIMIT 1)) AS project_id
  ) owner
  LEFT JOIN claude_account a ON a.account_id = owner.account_id
  LEFT JOIN project p ON p.project_id = owner.project_id;

-- Token and cost usage per chat and model (migration 015), now with the labels of the new tables.
--
-- Project names on dashboards (d_usage, d_chat): the label; else the name of the Git repository; else
-- the last part of the folder, except for a home folder (/home/<user>, /Users/<user>, /root,
-- C:\Users\<user>), whose last part is the person's login name: that one is shown as a neutral
-- project-<6 hex>, like an account without a label.
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
       c.started_at::date AS day,
       COALESCE(c.project_label,
                CASE WHEN c.project_kind = 'git_remote' THEN c.project_name END,
                CASE WHEN c.folder ~* '^(/home/[^/]+|/Users/[^/]+|/root|[a-z]:[\\/]Users[\\/][^\\/]+)[\\/]*$'
                     THEN 'project-' || left(md5(c.folder), 6) END,  -- a home folder: see above
                c.project_name,
                NULLIF(regexp_replace(rtrim(c.folder, '/\'), '^.*[/\\]', ''), ''),
                'unknown') AS project,
       COALESCE(c.account_label,
                'account-' || left(md5(lower(c.account_email)), 6),
                'unknown') AS account,
       COALESCE(c.surface, 'unknown') AS surface,
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
  JOIN chat c ON c.chat_id = u.session_id;

-- One row per slash command typed (/model, /compact ...): its name and the arguments given.
CREATE VIEW v_slash_command AS
SELECT e.event_id,
       e.session_id,
       e.occurred_at,
       e.is_abandoned,
       substring(t.text FROM '<command-name>\s*([^<]*?)\s*</command-name>') AS name,
       substring(t.text FROM '<command-args>(.*?)</command-args>') AS arguments
  FROM event e
  CROSS JOIN LATERAL (
      SELECT CASE WHEN jsonb_typeof(e.payload #> '{message,content}') = 'string'
                  THEN e.payload #>> '{message,content}'
                  ELSE (SELECT b ->> 'text'
                          FROM jsonb_array_elements(
                                   CASE WHEN jsonb_typeof(e.payload #> '{message,content}') = 'array'
                                        THEN e.payload #> '{message,content}' ELSE '[]' END) b
                         WHERE b ->> 'type' = 'text' LIMIT 1)
             END AS text) t
 WHERE e.event_type = 'slash_command';

-- One row per Workflow run: the sub-agents it started (agent_run.workflow_run_id) and the Workflow
-- tool call that launched it (its result names the run: toolUseResult.runId).
CREATE VIEW v_workflow_run AS
WITH runs AS (
    SELECT session_id, workflow_run_id FROM agent_run WHERE workflow_run_id IS NOT NULL
    UNION
    SELECT session_id, payload #>> '{toolUseResult,runId}' FROM event
     WHERE event_type = 'tool_result' AND payload #>> '{toolUseResult,runId}' IS NOT NULL
)
SELECT r.session_id,
       r.workflow_run_id,
       COALESCE(agents.workflow_name, launch.workflow_name) AS workflow_name,
       launch.status,
       COALESCE(agents.agent_count, 0) AS agent_count,
       agents.started_at,
       agents.ended_at,
       COALESCE(agents.event_count, 0) AS event_count,
       launch.call_event_id,
       launch.result_event_id
  FROM runs r
  LEFT JOIN LATERAL (
      SELECT max(a.workflow_name) AS workflow_name, count(*) AS agent_count,
             min(a.started_at) AS started_at, max(a.ended_at) AS ended_at,
             sum(a.event_count) AS event_count
        FROM agent_run a
       WHERE a.session_id = r.session_id AND a.workflow_run_id = r.workflow_run_id) agents ON TRUE
  LEFT JOIN LATERAL (
      SELECT res.event_id AS result_event_id,
             res.payload #>> '{toolUseResult,workflowName}' AS workflow_name,
             res.payload #>> '{toolUseResult,status}' AS status,
             (SELECT c.event_id FROM event c
               WHERE c.session_id = res.session_id AND c.event_type = 'tool_call'
                 AND c.tool_use_ids && res.tool_use_ids
               ORDER BY c.event_id LIMIT 1) AS call_event_id
        FROM event res
       WHERE res.session_id = r.session_id AND res.event_type = 'tool_result'
         AND res.payload #>> '{toolUseResult,runId}' = r.workflow_run_id
       ORDER BY res.event_id LIMIT 1) launch ON TRUE;

-- Dashboard view (rule of migration 013: counts and short categorical text only). One row per chat
-- that has messages: who (label or neutral name), which project, and how much happened in it.
CREATE VIEW d_chat AS
SELECT c.chat_id,
       c.started_at::date AS day,
       COALESCE(c.account_label,
                'account-' || left(md5(lower(c.account_email)), 6),
                'unknown') AS account,
       COALESCE(c.project_label,
                CASE WHEN c.project_kind = 'git_remote' THEN c.project_name END,
                CASE WHEN c.folder ~* '^(/home/[^/]+|/Users/[^/]+|/root|[a-z]:[\\/]Users[\\/][^\\/]+)[\\/]*$'
                     THEN 'project-' || left(md5(c.folder), 6) END,  -- a home folder: see above
                c.project_name,
                NULLIF(regexp_replace(rtrim(c.folder, '/\'), '^.*[/\\]', ''), ''),
                'unknown') AS project,
       COALESCE(c.surface, 'unknown') AS surface,
       c.session_count AS sessions,
       (SELECT count(*) FROM event e
         WHERE e.session_id = c.chat_id AND e.event_type = 'prompt' AND e.actor = 'human'
           AND NOT e.is_abandoned AND split_part(e.raw_object_key, '/', 3) = 'main') AS prompts,
       (SELECT COALESCE(sum(d.total_tokens), 0)::bigint FROM d_usage d WHERE d.session_id = c.chat_id)
           AS total_tokens,
       (SELECT (ARRAY['low', 'medium', 'high', 'xhigh', 'max'])[
                   max(array_position(ARRAY['low', 'medium', 'high', 'xhigh', 'max'], u.effort))]
          FROM model_usage u WHERE u.session_id = c.chat_id) AS effort,
       (SELECT COALESCE(sum(t.tests_passed), 0)::bigint FROM test_run t WHERE t.session_id = c.chat_id)
           AS tests_passed,
       (SELECT COALESCE(sum(t.tests_failed), 0)::bigint FROM test_run t WHERE t.session_id = c.chat_id)
           AS tests_failed,
       (SELECT count(*) FROM correction k WHERE k.session_id = c.chat_id) AS corrections,
       (SELECT count(*) FROM decision d WHERE d.session_id = c.chat_id AND d.status <> 'rejected')
           AS decisions
  FROM chat c
 WHERE NOT c.is_empty;

-- The roles that could read the old d_usage may read the new d_usage and d_chat.
DO $$
DECLARE
    reader REGROLE;
BEGIN
    FOR reader IN SELECT grantee::regrole FROM d_usage_reader LOOP
        EXECUTE 'GRANT SELECT ON d_usage, d_chat TO ' || reader::text;
    END LOOP;
END
$$;
