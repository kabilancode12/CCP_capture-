-- Undo 006_model_usage_and_cost.

DROP TABLE session_model_cost;
DROP TABLE session_cost;
DROP TABLE model_usage;
DROP FUNCTION json_bigint(JSONB);
