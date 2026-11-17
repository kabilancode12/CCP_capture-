-- 025_decision_reasoning: a decision is also advice, a bug cause or a plan, not only a code change.
--
-- Where a chat only gave advice ("is this a good design?"), the decision keeps the short answer,
-- when each option fits, the options not chosen and why, and what Claude still waits for, as the
-- exact words of the transcript. ``outcome`` says what became of the decision: advice that is
-- followed, not followed or changed later by a decision of a later chat is linked through the
-- developer's prompt (``follows_*``): ``decision_id`` changes when decisions are rewritten, the
-- prompt's transcript entry does not. Plain words are the values, so that a person can filter them.
-- Free text and lists live in ``details`` (display only).

ALTER TABLE decision
    ADD COLUMN kind                TEXT NOT NULL DEFAULT 'code change'
        CHECK (kind IN ('code change', 'advice', 'bug cause', 'plan')),
    ADD COLUMN raised_by           TEXT
        CHECK (raised_by IN ('developer asked', 'claude suggested', 'claude disagreed')),
    ADD COLUMN outcome             TEXT NOT NULL DEFAULT 'done'
        CHECK (outcome IN ('waiting', 'done', 'followed', 'not followed', 'changed later',
                           'postponed')),
    ADD COLUMN follows_session_id  TEXT,
    ADD COLUMN follows_prompt_uuid TEXT,
    ADD COLUMN details             JSONB NOT NULL DEFAULT '{}';

CREATE INDEX decision_follows_idx ON decision (follows_session_id, follows_prompt_uuid);
CREATE INDEX decision_outcome_idx ON decision (outcome) WHERE outcome = 'waiting';

-- The prompt version a chat without decisions was last looked at with: a newer prompt looks again.
ALTER TABLE session ADD COLUMN decisions_checked_version TEXT;
