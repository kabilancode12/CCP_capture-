-- Decisions keep Claude's own thinking notes and what the two model calls cost.
--
-- thinking:  [{"text": "<the note>", "key": "<raw object key>", "line": <line number>}]: the
--            thinking summaries (Claude Code's showThinkingSummaries) of the lines that explain the
--            decision. The text is copied from the recorded data, never written by the model.
-- llm_usage: what the run that wrote the decision used, one entry per call:
--            {"write": {"model", "input_tokens", "output_tokens", "cache_read_tokens",
--                       "cache_creation_tokens", "cost_usd", "seconds"}, "review": {...}}
--            The same value is on every decision of one run.

ALTER TABLE decision
    ADD COLUMN thinking  JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN llm_usage JSONB NOT NULL DEFAULT '{}';
