-- 002_session_activity_and_job_dedup: what the ingestion API and the idle-session closer need.
--
-- * session.status / last_activity_at: a session is 'open' while chunks keep arriving and is
--   marked 'closed' after a period without activity (task 1.6). A late chunk reopens it.
-- * One queued index job per session: many chunks of one session should not queue many jobs.

ALTER TABLE session
    ADD COLUMN status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    ADD COLUMN last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- The idle-session closer looks for open sessions that have been quiet for a while.
CREATE INDEX session_open_activity_idx ON session (last_activity_at) WHERE status = 'open';

-- At most one *queued* index job per session; a running or finished job does not block a new one.
CREATE UNIQUE INDEX job_one_queued_index_per_session
    ON job (session_id) WHERE job_type = 'index_session' AND status = 'queued';
