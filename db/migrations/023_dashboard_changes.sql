-- 023_dashboard_changes: a dashboard view of who changed the code (the capture chain, migration 021).
--
-- Rule of migration 013: counts and short categorical text only, never a file name, a prompt or a
-- commit message. One row per capture commit, with the chat's account label and project as in d_chat.
-- After applying it run `ccp-db create-reader` (and `create-auditor`) again so that the roles can read it.

CREATE VIEW d_change AS
SELECT s.captured_at::date AS day,
       c.account,
       c.project,
       s.actor,
       COALESCE(s.model, '(none)') AS model,
       COALESCE(s.tool, '(none)') AS tool,
       s.files_total AS files
  FROM shadow_commit s
  JOIN d_chat c ON c.chat_id = s.session_id;
