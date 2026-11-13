-- 012_commit_link: which session produced a developer's commit (Step 3, task 3.2).
--
-- Filled by `ccp-audit link-commits`. A commit is linked to a session by `hunk_match`: the lines the
-- commit adds are compared with the lines that the session's recorded AI edits added. `confidence` is
-- the share of the commit's added lines that the session's edits explain (0 to 1). A commit can link
-- to several sessions (work spread over more than one), each with its own share.
-- `git_note` is reserved for links written at commit time (not built yet).

CREATE TABLE commit_link (
    commit_sha    TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES session ON DELETE CASCADE,
    method        TEXT NOT NULL CHECK (method IN ('git_note', 'hunk_match')),
    confidence    NUMERIC(4, 3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    matched_lines INTEGER NOT NULL,               -- commit lines explained by this session's edits
    total_lines   INTEGER NOT NULL,               -- lines the commit adds that were compared
    committed_at  TIMESTAMPTZ,
    linked_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (commit_sha, session_id)
);
CREATE INDEX commit_link_session_idx ON commit_link (session_id);
