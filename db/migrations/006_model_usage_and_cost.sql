-- 006_model_usage_and_cost: which model was used, how many tokens, and what it cost (task 2.3).
--
-- Three tables, all filled by the indexer and rebuildable from `event`:
--
-- * model_usage: one row per model *response* (API message). Claude Code writes one transcript
--   entry per content block, and every one of them repeats the same usage, so the rows are
--   de-duplicated by message id; summing the entries would count a response several times
--   (observed: about two thirds of the assistant entries are repeats).
-- * session_cost: the session totals, read from the transcript's latest `cost-state` entry.
-- * session_model_cost: the same, per model. This is the only place helper models appear (for
--   example the small model that writes titles), and the only place cost is recorded.
--
-- Cost is READ from the transcript, never computed from a price list. It can differ from the sum
-- of model_usage: helper models and sub-agent work that has no entry in the main transcript are
-- counted in cost-state only.

-- The whole number held in a JSON value, or NULL if it is missing, not a number or too large for
-- a BIGINT. Used by the indexer so that an odd value in one transcript line cannot make a whole
-- refresh fail.
CREATE FUNCTION json_bigint(value JSONB) RETURNS BIGINT
    LANGUAGE SQL IMMUTABLE
    RETURN CASE WHEN jsonb_typeof(value) = 'number' AND abs((value #>> '{}')::numeric) < 9e18
                THEN (value #>> '{}')::numeric::bigint END;

CREATE TABLE model_usage (
    session_id            TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    message_id            TEXT NOT NULL,         -- the API message id (the entry uuid if it has none)
    event_id              BIGINT NOT NULL REFERENCES event ON DELETE CASCADE,  -- its first entry
    model                 TEXT,
    input_tokens          BIGINT,
    output_tokens         BIGINT,                -- includes thinking tokens
    thinking_tokens       BIGINT,
    cache_read_tokens     BIGINT,
    cache_creation_tokens BIGINT,
    PRIMARY KEY (session_id, message_id)
);
CREATE INDEX model_usage_event_idx ON model_usage (event_id);

CREATE TABLE session_cost (
    session_id             TEXT PRIMARY KEY REFERENCES session ON DELETE CASCADE,
    total_cost_usd         NUMERIC(12, 6),
    has_unknown_model_cost BOOLEAN,              -- true: some model had no known price, total is partial
    lines_added            BIGINT,
    lines_removed          BIGINT,
    api_duration_ms        BIGINT,
    tool_duration_ms       BIGINT
);

CREATE TABLE session_model_cost (
    session_id            TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    model                 TEXT NOT NULL,
    input_tokens          BIGINT,
    output_tokens         BIGINT,
    thinking_tokens       BIGINT,
    cache_read_tokens     BIGINT,
    cache_creation_tokens BIGINT,
    web_search_requests   BIGINT,
    cost_usd              NUMERIC(12, 6),
    PRIMARY KEY (session_id, model)
);
