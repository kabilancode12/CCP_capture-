-- Undo 020_shell_file_change: shell changes go (they can be rebuilt), one row per result event again.

DELETE FROM file_change WHERE actor_class = 'ai_command';
ALTER TABLE file_change DROP CONSTRAINT file_change_event_path_key;
ALTER TABLE file_change ADD CONSTRAINT file_change_event_id_key UNIQUE (event_id);
