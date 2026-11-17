-- Undo 024_decision_prompt.

ALTER TABLE decision DROP COLUMN prompt_text, DROP COLUMN prompt_at, DROP COLUMN prompt_uuid;
