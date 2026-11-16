-- Decisions say who decided and which files each one changed.
--
-- decided_by: 'developer' when the developer's own words set the direction, 'claude' when Claude
--             chose by itself.
-- files:      the files the decision changed, as
--             [{"path": "Program.cs", "change": "created|modified|deleted", "via": "edit|shell"}].
--             "edit" comes from Claude's Edit and Write tools, "shell" from commands such as
--             `cat > file`, `sed -i` or `rm`; the names are read from the recorded data, never
--             written by the model.

ALTER TABLE decision
    ADD COLUMN decided_by TEXT CHECK (decided_by IN ('developer', 'claude')),
    ADD COLUMN files      JSONB NOT NULL DEFAULT '[]';
