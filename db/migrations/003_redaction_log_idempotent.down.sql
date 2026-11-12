-- Undo 003_redaction_log_idempotent.

ALTER TABLE redaction_log
    DROP CONSTRAINT redaction_log_hit_unique,
    DROP COLUMN occurrences;
