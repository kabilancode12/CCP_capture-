-- Undo 002_session_activity_and_job_dedup.

DROP INDEX job_one_queued_index_per_session;
DROP INDEX session_open_activity_idx;
ALTER TABLE session
    DROP COLUMN last_activity_at,
    DROP COLUMN status;
