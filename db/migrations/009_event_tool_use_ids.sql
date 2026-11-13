-- 009_event_tool_use_ids: find "the event that holds this tool call" quickly (task 2.5).
--
-- Sub-agent runs and file changes both need the event that contains a given tool call (a tool_use
-- block with a given id). Searching every payload for it is slow on long sessions (tens of seconds
-- for a few thousand events), so the ids of the tool calls in an event are kept in an indexed column.

ALTER TABLE event ADD COLUMN tool_use_ids TEXT[] NOT NULL DEFAULT '{}';

-- Fill it for the events indexed so far: the id of every tool_use block of the message.
UPDATE event SET tool_use_ids = COALESCE(
    ARRAY(SELECT block ->> 'id'
            FROM jsonb_array_elements(payload #> '{message,content}') AS block
           WHERE block ->> 'type' = 'tool_use' AND block ->> 'id' IS NOT NULL),
    '{}')
 WHERE jsonb_typeof(payload #> '{message,content}') = 'array';

CREATE INDEX event_tool_use_ids_idx ON event USING GIN (tool_use_ids);
