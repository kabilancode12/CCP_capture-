"""Linking commits to sessions: a real Git repository, a real index, ``ccp-audit link-commits``."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ccp.audit.cli import main
from ccp.audit.repository import AuditRepository
from ccp.db.connection import connect
from ccp.index.repository import IndexRepository
from ccp.index.worker import process_next_job
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

AI_CODE = [
    "def total(items):",
    "    return sum(item.price for item in items)",
    "",
    "def label(item):",
    "    return f'{item.name}: {item.price}'",
]
HUMAN_CODE = ["# written by a person, not by Claude Code", "SETTING = 'by hand'"]


GIT = shutil.which("git") or "git"


def _git(repo: Path, *arguments: str, date: str | None = None) -> str:
    """Run git in the repository; ``date`` sets both the author and the committer date."""
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "", "GIT_CONFIG_SYSTEM": ""}
    if date:
        environment.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    done = subprocess.run(  # noqa: S603  (a test: fixed arguments, a throwaway repository)
        [
            GIT,
            "-C",
            str(repo),
            "-c",
            "user.name=Dev",
            "-c",
            "user.email=dev@example.com",
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, lines: list[str], message: str, date: str) -> str:
    (repo / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "capture.git"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    return path


def _entry(kind: str, uuid: str, parent: str | None, minute: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": f"2026-07-01T10:{minute:02d}:00.000Z",
        "cwd": "/work/app",
        **extra,
    }


def _transcript() -> bytes:
    """One session in which the AI creates src/shop.py with AI_CODE."""
    create = {
        "type": "create",
        "filePath": "/work/app/shop.py",
        "content": "\n".join(AI_CODE) + "\n",
        "structuredPatch": [],
    }
    entries = [
        _entry("user", "p1", None, 0, message={"content": "Write the shop module"}),
        _entry(
            "assistant", "c1", "p1", 1,
            message={"content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {}}]},
        ),
        _entry(
            "user", "r1", "c1", 2,
            message={"content": [{"type": "tool_result", "tool_use_id": "t1"}]}, toolUseResult=create,
        ),
    ]  # fmt: skip
    return ("".join(json.dumps(entry) + "\n" for entry in entries)).encode()


SESSION = "sess-linking"


@pytest.fixture
def indexed(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    object_store.put(transcript_chunk_key(SESSION, "main", 0), _transcript())
    PostgresRepository(database).record_chunk(SESSION)
    assert process_next_job(object_store, IndexRepository(database)) is True
    return database


@pytest.fixture
def environment(indexed: DatabaseSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "POSTGRES_HOST": indexed.host,
        "POSTGRES_PORT": str(indexed.port),
        "POSTGRES_DB": indexed.name,
        "POSTGRES_USER": indexed.user,
        "POSTGRES_PASSWORD": indexed.password.reveal(),
    }.items():
        monkeypatch.setenv(name, value)


def _links(database: DatabaseSettings) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT substr(commit_sha, 1, 7), session_id, method, confidence::float8, "
            "matched_lines, total_lines FROM commit_link ORDER BY committed_at, commit_sha"
        )
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def test_a_commit_of_ai_code_is_linked_and_a_commit_by_hand_is_not(
    environment: None, indexed: DatabaseSettings, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ai_commit = _commit(repo, "shop.py", AI_CODE, "Add the shop", "2026-07-01T11:00:00Z")
    by_hand = _commit(repo, "settings.py", HUMAN_CODE, "Tweak settings", "2026-07-01T11:30:00Z")

    assert main(["link-commits", "--repo", str(repo), "--all-refs"]) == 0

    out = capsys.readouterr().out
    assert f"{ai_commit[:10]}  sess-lin 100%" in out
    assert by_hand[:10] not in out
    assert "1 of 2 commit(s) linked to a session." in out
    # shop.py has 5 lines, 4 of them long enough to compare (the blank one is ignored).
    assert _links(indexed) == [(ai_commit[:7], SESSION, "hunk_match", 1.0, 4, 4)]


def test_a_commit_that_mixes_ai_and_human_code_gets_a_share(
    environment: None, indexed: DatabaseSettings, repo: Path
) -> None:
    mixed = _commit(
        repo, "shop.py", [*AI_CODE, *HUMAN_CODE], "Shop and settings", "2026-07-01T11:00:00Z"
    )

    assert main(["link-commits", "--repo", str(repo), "--all-refs"]) == 0

    assert _links(indexed) == [(mixed[:7], SESSION, "hunk_match", 0.667, 4, 6)]


def test_a_commit_made_before_the_edit_is_not_linked(
    environment: None, indexed: DatabaseSettings, repo: Path
) -> None:
    _commit(repo, "shop.py", AI_CODE, "Same code, but earlier", "2026-07-01T09:00:00Z")

    assert main(["link-commits", "--repo", str(repo), "--all-refs"]) == 0

    assert _links(indexed) == []


def test_the_default_references_are_the_capture_branch_backups(
    environment: None, indexed: DatabaseSettings, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit(repo, "shop.py", AI_CODE, "Add the shop", "2026-07-01T11:00:00Z")
    _git(repo, "update-ref", "refs/claude-capture/users/dev/heads/main", sha)
    _git(repo, "branch", "-m", "main", "elsewhere")  # the normal branch is not read by default

    assert main(["link-commits", "--repo", str(repo)]) == 0

    assert "1 of 1 commit(s) linked" in capsys.readouterr().out
    assert main(["link-commits", "--repo", str(repo), "--ref", "refs/heads/*"]) == 0


def test_linking_twice_changes_nothing(
    environment: None, indexed: DatabaseSettings, repo: Path
) -> None:
    _commit(repo, "shop.py", AI_CODE, "Add the shop", "2026-07-01T11:00:00Z")
    main(["link-commits", "--repo", str(repo), "--all-refs"])
    first = _links(indexed)

    assert main(["link-commits", "--repo", str(repo), "--all-refs"]) == 0

    assert _links(indexed) == first


def test_commit_shows_the_linked_sessions_and_session_commits_the_commits(
    environment: None, indexed: DatabaseSettings, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = _commit(repo, "shop.py", AI_CODE, "Add the shop", "2026-07-01T11:00:00Z")
    main(["link-commits", "--repo", str(repo), "--all-refs"])
    capsys.readouterr()

    assert main(["commit", sha[:8]]) == 0
    out = capsys.readouterr().out
    assert f"session {SESSION}  100% (4 of 4 lines)" in out
    assert "A link is a likelihood" in out

    assert main(["session-commits", "sess-link"]) == 0
    assert f"{sha[:10]}  2026-07-01 11:00  100% (4 of 4 lines)" in capsys.readouterr().out


def test_a_commit_without_a_link_says_so(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["commit", "abcdef1234"]) == 0
    assert "No session is linked to abcdef1234" in capsys.readouterr().out
    assert main(["session-commits", "nothing"]) == 0
    assert "No commit is linked" in capsys.readouterr().out


def test_a_short_commit_id_is_refused(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["commit", "abc"]) == 1
    assert "at least 7 characters" in capsys.readouterr().err


def test_a_confidence_outside_the_range_is_refused(
    environment: None, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["link-commits", "--repo", str(repo), "--min-confidence", "0"]) == 1
    assert "--min-confidence must be above 0" in capsys.readouterr().err


def test_a_folder_that_is_not_a_repository_is_reported(
    environment: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["link-commits", "--repo", str(tmp_path / "nothing"), "--all-refs"]) == 1
    assert "git log failed" in capsys.readouterr().err


def test_the_repository_returns_the_added_lines_of_the_edits(indexed: DatabaseSettings) -> None:
    lines = AuditRepository(indexed).ai_added_lines()

    assert [line.text for line in lines] == AI_CODE
    assert {line.session_id for line in lines} == {SESSION}
