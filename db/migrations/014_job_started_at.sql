-- 014_job_started_at: when a job was taken, so that a job whose worker died can be found (Step 5).
--
-- A worker sets `status = 'running'` when it takes a job. If the worker crashes, the job used to
-- stay 'running' forever. The worker now also records the time it took the job; a job that has
-- been running for longer than a timeout (30 minutes) is put back in the queue by the next worker
-- that looks for work (or left 'failed' if it has used its attempts).
--
-- The event types added by the indexer at the same time (slash_command, command_output, meta,
-- agent_message) need no change here: event.event_type has no CHECK, and their actors (human,
-- system, subagent) are already allowed.

ALTER TABLE job ADD COLUMN started_at TIMESTAMPTZ;   -- NULL: never taken, or taken before 014
