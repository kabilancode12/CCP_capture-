-- Undo 017_decision_files: the decisions lose who decided and their files.

ALTER TABLE decision DROP COLUMN files, DROP COLUMN decided_by;
