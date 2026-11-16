"""The approach library: lineage, search, compare and restore (the 10 KB parser story)."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ccp.approaches import library
from ccp.approaches.lineage import refresh_lineage
from ccp.db.connection import connect
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.shadow import parse_shadow_commit

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_AUTHOR_NAME": "d", "GIT_AUTHOR_EMAIL": "d@x.org",
           "GIT_COMMITTER_NAME": "d", "GIT_COMMITTER_EMAIL": "d@x.org"}  # fmt: skip
    done = subprocess.run(  # noqa: S603  (fixed arguments in a test)
        ["/usr/bin/git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return done.stdout.strip()


def _commit(repo: Path, subject: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


def _decision(
    cursor: Any, session: str, seq: int, title: str, files: list[dict[str, str]], **extra: str
) -> int:
    cursor.execute(
        "INSERT INTO decision (session_id, seq, title, goal, chosen, reason, result, files, status, options) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'approved', %s::jsonb) RETURNING decision_id",
        (session, seq, title, extra.get("goal"), extra.get("chosen"), extra.get("reason"), extra.get("result"),
         json.dumps(files), json.dumps(extra.get("options", "[]") and ["simple parser: small", "streaming JSON: big"])),
    )  # fmt: skip
    return int(cursor.fetchone()[0])


def test_a_removed_approach_is_found_compared_and_restored(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    repo = tmp_path / "capture"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    (repo / "parser.py").write_text(
        "def parse(text):\n    return text.split(',')  # small parser\n"
    )
    first = _commit(repo, "[AI] Write parser.py")
    (repo / "parser.py").unlink()
    (repo / "json_parser.py").write_text(
        "import json\n\ndef parse(stream):\n    return json.load(stream)\n"
    )
    second = _commit(repo, "[AI] Replace with json parser")

    sink = PostgresRepository(database)
    for sha, session, when, files in (
        (first, "chat-a", "2026-11-16T10:00:00+00:00", [{"path": "parser.py", "state": "A"}]),
        (second, "chat-b", "2026-11-17T10:00:00+00:00",
         [{"path": "parser.py", "state": "D"}, {"path": "json_parser.py", "state": "A"}]),
    ):  # fmt: skip
        sink.record_shadow_commit(
            session,
            parse_shadow_commit(
                {
                    "sha": sha,
                    "actor": "ai_edit",
                    "captured_at": when,
                    "files": files,
                    "files_total": len(files),
                }
            ),
        )

    connection = connect(database)
    try:
        cursor = connection.cursor()
        small = _decision(cursor, "chat-a", 1, "Simple parser (10 KB)", [{"path": "parser.py", "change": "created"}],
                          goal="parse small config files", chosen="split on commas", reason="files are tiny")  # fmt: skip
        big = _decision(cursor, "chat-b", 1, "Streaming JSON parser (10 MB)",
                        [{"path": "parser.py", "change": "deleted"}, {"path": "json_parser.py", "change": "created"}],
                        goal="parse 10 MB files", chosen="json streaming", reason="input grew to 10 MB")  # fmt: skip
        assert refresh_lineage(connection) == 2

        old, new = library.get(connection, small), library.get(connection, big)
        assert old is not None and new is not None
        assert (old.lifecycle, old.code_sha, old.removed_in_sha, old.removed_by_id) == (
            "superseded",
            first,
            second,
            big,
        )
        assert (new.lifecycle, new.supersedes_id, new.code_sha) == ("active", small, second)

        found = library.search(connection, "parser 10")
        assert {a.decision_id for a in found} == {small, big}
        removed = library.search(connection, "parser", only_removed=True)
        assert [a.decision_id for a in removed] == [small]

        target = tmp_path / "project"
        written = library.restore(
            connection, repo, old, target, by="devC", purpose="parse small CLI config"
        )
        assert written == [target / "parser.py"]
        assert "small parser" in (target / "parser.py").read_text()
        with pytest.raises(library.RestoreError, match="exists"):
            library.restore(connection, repo, old, target, by="devC", purpose="again")
        assert "json" in "\n".join(library.diff(repo, old, new))

        again = library.get(connection, small)
        assert again is not None and again.reuses == 1 and again.reuse_log[0]["reused_by"] == "devC"
    finally:
        connection.rollback()
        connection.close()
