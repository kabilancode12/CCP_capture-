-- Undo 009_event_tool_use_ids.

DROP INDEX event_tool_use_ids_idx;
ALTER TABLE event DROP COLUMN tool_use_ids;
