-- Undo 018_decision_thinking_usage: the decisions lose their thinking notes and usage record.

ALTER TABLE decision DROP COLUMN llm_usage, DROP COLUMN thinking;
