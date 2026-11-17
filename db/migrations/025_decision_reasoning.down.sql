-- Undo 025_decision_reasoning.

ALTER TABLE session DROP COLUMN decisions_checked_version;
DROP INDEX decision_outcome_idx;
DROP INDEX decision_follows_idx;
ALTER TABLE decision
    DROP COLUMN details, DROP COLUMN follows_prompt_uuid, DROP COLUMN follows_session_id,
    DROP COLUMN outcome, DROP COLUMN raised_by, DROP COLUMN kind;
