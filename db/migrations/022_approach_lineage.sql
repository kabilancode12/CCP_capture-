-- 022_approach_lineage: what a decision's code became, and where it was reused.
--
-- An *approach* is a decision together with its code. The capture chain (migration 021) keeps the
-- code of every step, even when it was never committed, so the code of a removed approach can be
-- found again. These columns say where the code is (`code_sha`), whether it was later removed and
-- by which decision, and `approach_reuse` records each time someone brought an approach back.
-- All the decision columns are worked out by `ccp-approaches refresh` and can be recomputed.

ALTER TABLE decision
    ADD COLUMN lifecycle              TEXT NOT NULL DEFAULT 'active'
                                      CHECK (lifecycle IN ('active', 'superseded', 'removed')),
    ADD COLUMN code_sha               TEXT,         -- the capture commit with the decision's code
    ADD COLUMN removed_in_sha         TEXT,         -- the capture commit that deleted it
    ADD COLUMN removed_at             TIMESTAMPTZ,
    ADD COLUMN removed_by_decision_id BIGINT REFERENCES decision (decision_id) ON DELETE SET NULL,
    ADD COLUMN supersedes_decision_id BIGINT REFERENCES decision (decision_id) ON DELETE SET NULL;

CREATE INDEX decision_lifecycle_idx ON decision (lifecycle);

CREATE TABLE approach_reuse (
    reuse_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    decision_id    BIGINT REFERENCES decision (decision_id) ON DELETE SET NULL,
    approach_title TEXT,        -- kept, in case the decision is rewritten
    code_sha       TEXT,
    reused_by      TEXT,
    purpose        TEXT,
    target         TEXT,        -- where the files were put
    reused_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX approach_reuse_decision_idx ON approach_reuse (decision_id);
