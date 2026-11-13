-- 004_thinking: the model's reasoning blocks, one row per block (Step 2, task 2.1).
--
-- Claude Code 2.1.289 writes only the signature of a thinking block, not its text (observed, see
-- docs/verify/transcript-facts.md, fact 10). The table is built so that any text a Claude Code
-- version does write is kept, and "the model thought here" is recorded either way. The signature
-- itself is opaque and large, so it is not stored; it stays in the event's payload.
--
-- Thinking text is summarised reasoning, never proof of intent, and can contain anything the model
-- saw. Treat this table as restricted: give access only to roles that need it (Step 5).

CREATE TABLE thinking (
    event_id    BIGINT NOT NULL REFERENCES event ON DELETE CASCADE,
    block_index INTEGER NOT NULL,                -- position of the block within the message
    text        TEXT,                            -- NULL when Claude Code stored no text
    is_redacted BOOLEAN NOT NULL DEFAULT FALSE,  -- a redacted_thinking block (content withheld by the model)
    PRIMARY KEY (event_id, block_index)
);
