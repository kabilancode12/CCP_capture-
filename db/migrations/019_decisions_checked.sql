-- Remember when a chat was last given to the decisions run, even when the model found no decision
-- in it (a short question-and-answer chat). Without this the chat stayed "pending" and was sent to
-- the model again at every sweep.

ALTER TABLE session ADD COLUMN decisions_checked_at TIMESTAMPTZ;
