-- 024_decision_prompt: keep the developer's prompt with each decision.
--
-- A decision starts with something the developer asked. The prompt that was current at the first
-- evidence line of the decision is stored here (its full text, its time and its transcript entry),
-- so a decision can always be read together with what was asked, even after the index is rebuilt.
-- Filled when decisions are written, and for decisions that exist by the server at start
-- (ccp.decisions.repository.fill_missing_prompts).

ALTER TABLE decision
    ADD COLUMN prompt_text TEXT,
    ADD COLUMN prompt_at   TIMESTAMPTZ,
    ADD COLUMN prompt_uuid TEXT;
