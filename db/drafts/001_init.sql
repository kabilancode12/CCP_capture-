-- =====================================================================================
-- DRAFT: initial schema for the Claude Capture Platform.   *** DO NOT APPLY YET ***
--
-- Status : draft for review. It becomes a real migration in Step 2, after the facts in
--          docs/verify/transcript-facts.md are known.
-- Target : PostgreSQL 16 with the pgvector extension.
--
-- Conventions
--   * Column or table comments starting "[verify]" depend on an unconfirmed fact about what
--     Claude Code writes. Re-check them against transcript-facts.md before applying.
--   * Layer 2 tables (everything except knowledge_entry, redaction_log and job) are derived
--     data: they can be dropped and rebuilt from the raw transcripts in MinIO.
--   * Every event points back to its raw transcript: raw_object_key + raw_line_number.
-- =====================================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------------ sessions

-- One row per Claude Code session.
CREATE TABLE session (
    session_id      TEXT PRIMARY KEY,            -- Claude Code's session id
    capture_id      UUID,                        -- id written into the conversation marker
    developer_email TEXT,
    repository      TEXT,
    branch          TEXT,
    start_commit    TEXT,
    start_source    TEXT,                        -- [verify] startup | resume | clear | compact
    end_reason      TEXT,                        -- [verify] may be unknown if SessionEnd is missed
    is_private      BOOLEAN NOT NULL DEFAULT FALSE,  -- private mode: minimal record only
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ
);

-- Who ran the session, with which Claude Code, and where.
CREATE TABLE session_env (
    session_id          TEXT PRIMARY KEY REFERENCES session,
    claude_code_version TEXT,
    account_name        TEXT,                    -- [verify] where these are stored locally
    account_email       TEXT,                    -- [verify]
    organisation        TEXT,                    -- [verify]
    auth_type           TEXT,                    -- subscription | api_key  [verify]
    os_user             TEXT,
    hostname            TEXT,
    run_mode            TEXT,                    -- interactive | headless | sdk  [verify]
    surface             TEXT,                    -- terminal | vscode | jetbrains | desktop | web  [verify]
    shell               TEXT,                    -- bash | zsh | git-bash | wsl | cmd | powershell  [verify]
    os_name             TEXT,
    os_version          TEXT,
    cpu_architecture    TEXT,
    access_path         TEXT                     -- local | ssh | container | remote  [verify]
);

-- Links between sessions (resume, continue, after-compact, same branch).
CREATE TABLE session_link (
    from_session_id TEXT NOT NULL REFERENCES session,
    to_session_id   TEXT NOT NULL REFERENCES session,
    link_type       TEXT NOT NULL,               -- [verify] depends on how resume is recorded
    PRIMARY KEY (from_session_id, to_session_id, link_type)
);

-- ------------------------------------------------------------------ events

-- One row per message, tool call, permission decision, plan, to-do, compaction or toggle.
CREATE TABLE event (
    event_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session,
    entry_uuid      TEXT,                        -- the transcript entry's uuid
    parent_uuid     TEXT,                        -- [verify] position in the conversation tree
    is_abandoned    BOOLEAN NOT NULL DEFAULT FALSE,  -- [verify] set for rewound branches
    event_type      TEXT NOT NULL,               -- prompt | response | tool_call | permission | ...
    actor           TEXT NOT NULL CHECK (actor IN ('human', 'ai', 'subagent', 'system')),
    occurred_at     TIMESTAMPTZ,
    payload         JSONB NOT NULL,              -- the raw entry (redacted before upload)
    raw_object_key  TEXT NOT NULL,               -- MinIO object holding the transcript
    raw_line_number INTEGER NOT NULL             -- line within that object
);
CREATE INDEX event_session_idx ON event (session_id, occurred_at);

-- Model, tokens and cost per assistant message.
CREATE TABLE model_usage (
    event_id      BIGINT PRIMARY KEY REFERENCES event,
    model         TEXT,                          -- [verify] present on assistant entries
    input_tokens  INTEGER,                       -- [verify]
    output_tokens INTEGER,
    cache_tokens  INTEGER,
    cost_usd      NUMERIC(12, 6)                 -- computed from a price table, not recorded
);

-- Each sub-agent run, linked to the call that launched it.
CREATE TABLE agent_run (
    agent_run_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session,
    launch_event_id BIGINT REFERENCES event,     -- the parent's Task call  [verify]
    agent_type      TEXT,
    instructions    TEXT,
    result          TEXT,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ
);

-- Thinking text. Restricted access: it is "summarised reasoning", never proof of intent.
CREATE TABLE thinking (
    event_id    BIGINT PRIMARY KEY REFERENCES event,
    text        TEXT,                            -- [verify] may be empty (signature only)
    is_redacted BOOLEAN NOT NULL DEFAULT FALSE   -- [verify] redacted_thinking blocks
);

-- Manual or automatic compaction of the conversation.
CREATE TABLE compaction (
    compaction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES session,
    event_id      BIGINT REFERENCES event,
    trigger       TEXT,                          -- manual | auto  [verify]
    summary       TEXT                           -- [verify]
);

-- ------------------------------------------------------------------ code changes

-- A change to a file, with who made it. Hunks are stored as JSON.
CREATE TABLE file_change (
    file_change_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES session,
    path           TEXT NOT NULL,
    change_type    TEXT NOT NULL CHECK (change_type IN ('added', 'modified', 'deleted')),
    actor_class    TEXT NOT NULL CHECK (actor_class IN ('ai_edit', 'ai_command', 'human')),
    explaining_event_id BIGINT REFERENCES event, -- the event that explains why
    snapshot_ref   TEXT,                         -- hidden Git ref of the snapshot
    hunks          JSONB                         -- [{old_start, old_lines, new_start, new_lines}]
);
CREATE INDEX file_change_path_idx ON file_change (path);

-- Links a developer's commit to the session that produced it.
CREATE TABLE commit_link (
    commit_sha   TEXT NOT NULL,
    session_id   TEXT NOT NULL REFERENCES session,
    method       TEXT NOT NULL CHECK (method IN ('git_note', 'hunk_match')),
    confidence   NUMERIC(3, 2),
    PRIMARY KEY (commit_sha, session_id)
);

-- A test run found in a session (pass/fail level).
CREATE TABLE test_run (
    test_run_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES session,
    event_id     BIGINT REFERENCES event,
    command      TEXT,
    framework    TEXT,
    passed       BOOLEAN
);

-- CLAUDE.md, memory, agent definitions and settings in effect, versioned by content hash.
CREATE TABLE context_snapshot (
    session_id   TEXT NOT NULL REFERENCES session,
    kind         TEXT NOT NULL,                  -- claude_md | memory | agent | settings | mcp
    content_hash TEXT NOT NULL,
    object_key   TEXT NOT NULL,                  -- where the copy is stored
    PRIMARY KEY (session_id, kind, content_hash)
);

-- ------------------------------------------------------------------ knowledge and governance

-- A distilled, reviewed answer. Layer 3: not rebuilt from raw data.
CREATE TABLE knowledge_entry (
    entry_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       TEXT NOT NULL,
    problem     TEXT,
    cause       TEXT,
    fix         TEXT,
    gotchas     TEXT,
    evidence    JSONB,                           -- session ids, event ids, snapshot refs
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'superseded')),
    embedding   vector(1536),                    -- dimension depends on the model chosen later
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What was redacted, where and by which rule. Never stores the secret itself.
CREATE TABLE redaction_log (
    redaction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id   TEXT NOT NULL,
    rule_id      TEXT NOT NULL,
    location     TEXT NOT NULL,                  -- object key + line number
    redacted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Work queue for indexing and distillation (PostgreSQL-based; no separate queue service).
CREATE TABLE job (
    job_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_type    TEXT NOT NULL,                   -- index_session | distil_session
    session_id  TEXT,
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
