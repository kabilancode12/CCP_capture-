-- 021_shadow_commit: the snapshots of the capture repository, linked to the chat that caused them.
--
-- Every change to a developer's working tree (a tool call, or a change made outside Claude) is one
-- commit in the capture repository. The client reports each one; this table is what lets the index
-- answer "which chat, prompt, tool call and model made this commit" and, with `git blame` on the
-- capture repository, "which commit wrote this line". Reported by the client, not rebuilt from the
-- transcripts, so `ccp-index rebuild` leaves it alone.

CREATE TABLE shadow_commit (
    sha             TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    parent_sha      TEXT,
    seq             INTEGER,
    actor           TEXT NOT NULL CHECK (actor IN ('human', 'ai_edit', 'ai_command', 'ai_other', 'subagent')),
    developer       TEXT,
    model           TEXT,
    tool            TEXT,
    target          TEXT,
    tool_use_id     TEXT,
    prompt_uuid     TEXT,
    prompt_excerpt  TEXT,
    agent           TEXT,
    event           TEXT,
    branch          TEXT,
    chain           TEXT,            -- the capture branch: ccp/<user>/<host>/<branch>
    captured_at     TIMESTAMPTZ NOT NULL,
    files           JSONB NOT NULL DEFAULT '[]',   -- [{path, state: A|M|D}]
    files_total     INTEGER NOT NULL DEFAULT 0,
    reported_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX shadow_commit_session_idx ON shadow_commit (session_id, captured_at);
CREATE INDEX shadow_commit_tool_use_idx ON shadow_commit (tool_use_id);
CREATE INDEX shadow_commit_chain_idx ON shadow_commit (chain, captured_at);
