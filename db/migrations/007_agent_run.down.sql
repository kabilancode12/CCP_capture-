-- Undo 007_agent_run. (The description rows that the up migration removed from `event` are
-- derived data and come back only if sessions are indexed again by the older indexer.)

DROP TABLE agent_run;
