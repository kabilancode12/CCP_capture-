-- Undo 001_initial_schema. Drops everything it created, in reverse dependency order.
-- WARNING: this deletes all captured session data.

DROP TABLE job;
DROP TABLE redaction_log;
DROP TABLE event;
DROP TABLE session_env;
DROP TABLE session;
