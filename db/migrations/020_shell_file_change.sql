-- 020_shell_file_change: a shell command can change several files.
--
-- Until now one result event gave one file_change row (UNIQUE event_id), which fits the Edit and
-- Write tools. Files written, changed or removed by a shell command (`cat > f`, `sed -i`, `rm`, ...)
-- are now recorded too, with actor_class `ai_command`, and one command may name several files, so
-- the key becomes (event_id, path).

ALTER TABLE file_change DROP CONSTRAINT file_change_event_id_key;
ALTER TABLE file_change ADD CONSTRAINT file_change_event_path_key UNIQUE (event_id, path);
