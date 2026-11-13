-- 005_compaction: one row per compaction of a conversation (Step 2, task 2.2).
--
-- A compaction replaces the earlier part of a conversation with a summary. Claude Code writes a
-- `compact_boundary` entry (with metadata: trigger and token counts), followed by a user entry that
-- holds the summary text. The raw lines stay in `event`; this table makes them easy to query.
-- It is filled by the indexer and can be rebuilt from `event`.

CREATE TABLE compaction (
    compaction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    event_id      BIGINT NOT NULL UNIQUE REFERENCES event ON DELETE CASCADE,  -- the boundary entry
    trigger       TEXT,                          -- observed: manual | auto
    pre_tokens    BIGINT,                        -- size of the conversation before
    post_tokens   BIGINT,                        -- size after
    summary       TEXT                           -- the summary entry's text; NULL until it is indexed
);
CREATE INDEX compaction_session_idx ON compaction (session_id);
