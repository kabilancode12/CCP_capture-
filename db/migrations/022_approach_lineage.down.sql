-- Undo 022_approach_lineage.

DROP TABLE approach_reuse;
DROP INDEX decision_lifecycle_idx;
ALTER TABLE decision
    DROP COLUMN lifecycle,
    DROP COLUMN code_sha,
    DROP COLUMN removed_in_sha,
    DROP COLUMN removed_at,
    DROP COLUMN removed_by_decision_id,
    DROP COLUMN supersedes_decision_id;
