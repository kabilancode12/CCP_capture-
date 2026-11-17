"""Exact line tracing: git blame on a capture chain plus the index (real Git, real PostgreSQL)."""

import subprocess
from pathlib import Path

import pytest

from ccp.audit.repository import AuditRepository
from ccp.audit.trace import describe, newest_chain_ref, trace_line
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.shadow import parse_shadow_commit

pytestmark = pytest.mark.integration

SESSION = "sess-trace"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(  # noqa: S603  (fixed arguments in a test)
        ["/usr/bin/git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo), **(env or {})},
    )  # fmt: skip
    return done.stdout.strip()


def _commit(repo: Path, text: str, subject: str, author: str) -> str:
    (repo / "parser.py").write_text(text, encoding="utf-8")
    _git(repo, "add", "parser.py")
    who = {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "d@example.org",
           "GIT_COMMITTER_NAME": "dev", "GIT_COMMITTER_EMAIL": "d@example.org"}  # fmt: skip
    _git(repo, "commit", "-q", "-m", subject, env=who)
    return _git(repo, "rev-parse", "HEAD")


def test_a_line_is_traced_to_the_capture_commit_and_its_cause(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    repo = tmp_path / "capture"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    old = _commit(repo, "old line\n", "an ordinary commit", "dev")
    ai = _commit(repo, "old line\nai line\n", "[AI · m] Edit parser.py", "Claude (m) for dev")
    person = _commit(repo, "old line\nai line\nperson line\n", "[Human · dev] x", "dev")
    _git(repo, "update-ref", "refs/heads/ccp/dev/host/work", person)

    sink = PostgresRepository(database)
    for sha, parent, actor in ((ai, old, "ai_edit"), (person, ai, "human")):
        sink.record_shadow_commit(
            SESSION,
            parse_shadow_commit(
                {"sha": sha, "parent_sha": parent, "actor": actor, "captured_at": "2026-11-16T10:00:00+00:00",
                 "files": [{"path": "parser.py", "state": "M"}], "files_total": 1, "model": "m",
                 "tool": "Edit", "tool_use_id": "toolu_1", "prompt_excerpt": "write a parser",
                 "developer": "dev", "agent": "main"}
            ),
        )  # fmt: skip
    index = AuditRepository(database)
    ref = newest_chain_ref(repo)
    assert ref == "refs/heads/ccp/dev/host/work"

    older = trace_line(repo, ref, "parser.py", 1, index)  # not a capture commit
    assert older.shadow is None and older.commit == old
    assert "older than capture" in "\n".join(describe(older, "parser.py", 1))

    by_ai = trace_line(repo, ref, "parser.py", 2, index)
    assert by_ai.shadow is not None and by_ai.shadow.actor == "ai_edit" and by_ai.commit == ai
    text = "\n".join(describe(by_ai, "parser.py", 2))
    assert "Claude (m)" in text and "prompt: write a parser" in text and "toolu_1" in text

    by_person = trace_line(repo, ref, "parser.py", 3, index)
    assert by_person.shadow is not None and by_person.shadow.actor == "human"
    assert "a person (outside Claude)" in "\n".join(describe(by_person, "parser.py", 3))

    rows = index.snapshots_of("sess-tr")
    assert [r[1] for r in rows] == ["ai_edit", "human"]
