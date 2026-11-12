-- 003_redaction_log_idempotent: make reporting redactions safe to repeat.
--
-- The capture client reports its redaction hits after uploading a chunk, and repeats the whole
-- step if anything fails (see docs/capture-client.md). A repeated report must not create duplicate
-- rows, so a hit is identified by (session, rule, location) and carries a count: several hits of
-- one rule in the same place (two emails in one text) are one row with occurrences = 2.
-- The location is "<chunk object key>:<line in the chunk>:<JSON path>"; never the redacted value.

ALTER TABLE redaction_log
    ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1 CHECK (occurrences > 0),
    ADD CONSTRAINT redaction_log_hit_unique UNIQUE (session_id, rule_id, location);
