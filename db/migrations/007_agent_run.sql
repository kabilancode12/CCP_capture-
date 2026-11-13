-- 007_agent_run: one row per sub-agent run (Step 2, task 2.4).
--
-- A sub-agent has its own transcript (stream `agent-<id>`) and a small description (stream
-- `agent-<id>.meta`). The description says which call of the parent launched it. The indexer fills
-- this table from both, and it can be rebuilt from the stored transcripts.
--
-- A sub-agent can launch further sub-agents: `launch_event_id` may point at an event of another
-- sub-agent, and `spawn_depth` says how deep the agent is (1 = launched by the main conversation).

CREATE TABLE agent_run (
    session_id      TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    agent_id        TEXT NOT NULL,                -- the id in the stream name
    agent_type      TEXT,                         -- from the description
    description     TEXT,                         -- the short task title the parent gave
    model           TEXT,
    spawn_depth     INTEGER,
    tool_use_id     TEXT,                         -- the launching tool call's id (from the description)
    launch_event_id BIGINT REFERENCES event ON DELETE SET NULL,  -- the launching tool call
    instructions    TEXT,                         -- the prompt the parent gave (from that call)
    result          TEXT,                         -- the agent's last text reply
    started_at      TIMESTAMPTZ,                  -- first and last entry of its own transcript
    ended_at        TIMESTAMPTZ,
    event_count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, agent_id)
);
CREATE INDEX agent_run_launch_idx ON agent_run (launch_event_id);

-- Before this migration the indexer stored each description as if it were a transcript line (an
-- 'unknown' event). Descriptions are not events: remove those rows. The indexer re-reads the
-- descriptions into agent_run at the next job for each session.
DELETE FROM event WHERE split_part(raw_object_key, '/', 3) ~ '^agent-.*[.]meta$';
