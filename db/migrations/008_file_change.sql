-- 008_file_change: one row per AI edit of a file (Step 2, task 2.5).
--
-- Filled by the indexer from the result entries of the Edit and Write tools, which carry the changed
-- line ranges (fact 11 in docs/verify/transcript-facts.md). Rebuildable from `event`.
--
-- Not covered: files changed by a shell command (`ai_command`) and changes made by a person
-- (`human`). The column allows those values for later; today every row is `ai_edit`.

CREATE TABLE file_change (
    file_change_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    event_id        BIGINT NOT NULL UNIQUE REFERENCES event ON DELETE CASCADE,  -- the tool result
    tool_use_id     TEXT,                         -- the id of the call that made the change
    call_event_id   BIGINT REFERENCES event ON DELETE SET NULL,  -- that call (the AI's decision)
    tool            TEXT,                         -- Edit | Write | ... (from the call)
    prompt_event_id BIGINT REFERENCES event ON DELETE SET NULL,  -- the developer's prompt behind it
    path            TEXT NOT NULL,                -- relative to the session folder when inside it
    absolute_path   TEXT NOT NULL,                -- as reported (contains the user name: restricted)
    change_type     TEXT NOT NULL CHECK (change_type IN ('added', 'modified', 'deleted')),
    actor_class     TEXT NOT NULL DEFAULT 'ai_edit'
                    CHECK (actor_class IN ('ai_edit', 'ai_command', 'human')),
    user_modified   BOOLEAN,                      -- the developer changed the proposal before accepting
    lines_added     INTEGER NOT NULL DEFAULT 0,
    lines_removed   INTEGER NOT NULL DEFAULT 0,
    hunks           JSONB NOT NULL DEFAULT '[]'   -- [{old_start, old_lines, new_start, new_lines}]
);
CREATE INDEX file_change_path_idx ON file_change (path);
CREATE INDEX file_change_session_idx ON file_change (session_id);
