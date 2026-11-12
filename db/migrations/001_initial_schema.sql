-- 001_initial_schema: the tables needed to capture and ingest sessions (Step 1).
--
-- Later steps add their own tables in new numbered files (indexer tables in Step 2, the
-- knowledge layer in Step 4). The full design is in db/drafts/001_init.sql.
--
-- Column comments marked "observed" were confirmed on Claude Code 2.1.289
-- (see docs/verify/transcript-facts.md).

-- One row per Claude Code session.
CREATE TABLE session (
    session_id      TEXT PRIMARY KEY,            -- Claude Code's session id
    capture_id      UUID,                        -- id written into the conversation marker
    developer_email TEXT,
    repository      TEXT,
    branch          TEXT,
    start_commit    TEXT,
    start_source    TEXT,                        -- observed: startup | resume | clear | compact
    end_reason      TEXT,                        -- observed: other | prompt_input_exit; NULL if SessionEnd never ran
    is_private      BOOLEAN NOT NULL DEFAULT FALSE,  -- private mode: minimal record only
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ
);

-- Who ran the session, with which Claude Code, and where. One row per session.
CREATE TABLE session_env (
    session_id          TEXT PRIMARY KEY REFERENCES session ON DELETE CASCADE,
    claude_code_version TEXT,
    entrypoint          TEXT,                    -- observed: cli | claude-vscode (CLAUDE_CODE_ENTRYPOINT)
    surface             TEXT,                    -- terminal | vscode | jetbrains | desktop | web
    run_mode            TEXT,                    -- interactive | headless | sdk
    tool_shell          TEXT,                    -- shell Claude uses for tools (transcript `environment` entry)
    hook_shell          TEXT,                    -- shell the capture hook runs in (e.g. git-bash)
    os_name             TEXT,
    os_version          TEXT,
    cpu_architecture    TEXT,
    access_path         TEXT,                    -- local | ssh | container | remote
    os_user             TEXT,
    hostname            TEXT,
    account_name        TEXT,
    account_email       TEXT,
    organisation_name   TEXT,
    organisation_uuid   TEXT,
    auth_type           TEXT                     -- subscription | api_key
);

-- One row per message, tool call, permission decision, plan, compaction or private-mode toggle.
-- Filled by the indexer (Step 2); created now so the ingestion code can already refer to it.
CREATE TABLE event (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    entry_uuid      TEXT,                        -- the transcript entry's uuid
    parent_uuid     TEXT,                        -- position in the conversation tree
    is_abandoned    BOOLEAN NOT NULL DEFAULT FALSE,  -- set for rewound branches
    event_type      TEXT NOT NULL,               -- prompt | response | tool_call | permission | ...
    actor           TEXT NOT NULL CHECK (actor IN ('human', 'ai', 'subagent', 'system')),
    occurred_at     TIMESTAMPTZ,
    payload         JSONB NOT NULL,              -- the raw entry (already redacted)
    raw_object_key  TEXT NOT NULL,               -- storage object holding the transcript
    raw_line_number INTEGER NOT NULL,            -- line within that object
    -- Re-indexing is safe: the same transcript line can never produce two events.
    UNIQUE (raw_object_key, raw_line_number)
);
CREATE INDEX event_session_idx ON event (session_id, occurred_at);

-- What was redacted, where and by which rule. Never stores the redacted value itself.
CREATE TABLE redaction_log (
    redaction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   TEXT NOT NULL,                  -- no foreign key: the log must survive a session delete
    rule_id      TEXT NOT NULL,
    location     TEXT NOT NULL,                  -- object key, line number and JSON path
    redacted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX redaction_log_session_idx ON redaction_log (session_id);

-- Work queue for indexing and (later) distillation. PostgreSQL-based: no separate queue service.
CREATE TABLE job (
    job_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type    TEXT NOT NULL,                   -- index_session | distil_session
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
-- Workers poll for the oldest queued job (SELECT ... FOR UPDATE SKIP LOCKED).
CREATE INDEX job_queue_idx ON job (status, created_at);
