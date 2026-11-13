-- 010_test_run: one row per test run found in a session (Step 2, task 2.6).
--
-- A test run is a shell call that starts a known test runner (pytest, npm test, dotnet test, ...),
-- with its outcome. The transcript records no exit code for a success: no error means passed, and a
-- failure's text starts with "Exit code <n>". Filled by the indexer, rebuildable from `event`.

-- event.tool_use_ids (migration 009) now also holds the ids a tool RESULT answers, so that a call
-- and its result can be found from each other quickly. Fill it for results indexed so far.
UPDATE event SET tool_use_ids = ARRAY(
        SELECT block ->> 'tool_use_id'
          FROM jsonb_array_elements(payload #> '{message,content}') AS block
         WHERE block ->> 'type' = 'tool_result' AND block ->> 'tool_use_id' IS NOT NULL)
 WHERE event_type = 'tool_result' AND jsonb_typeof(payload #> '{message,content}') = 'array';

CREATE TABLE test_run (
    test_run_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    call_event_id   BIGINT NOT NULL UNIQUE REFERENCES event ON DELETE CASCADE,  -- the shell call
    result_event_id BIGINT REFERENCES event ON DELETE SET NULL,                -- its result
    tool_use_id     TEXT NOT NULL,
    command         TEXT NOT NULL,                -- as given to the shell (cut to 2000 characters)
    framework       TEXT NOT NULL,                -- pytest | npm test | dotnet test | ...
    passed          BOOLEAN,                      -- NULL: cannot be told (interrupted, no exit code)
    exit_code       INTEGER
);
CREATE INDEX test_run_session_idx ON test_run (session_id);
