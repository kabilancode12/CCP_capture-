"""Git facts of a chat's folder: the remote without credentials, the identity and HEAD."""

import subprocess
from pathlib import Path

import pytest

from ccp.capture.gitinfo import GitFacts, read_git_facts, read_head, strip_credentials


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603  (fixed program, list of arguments)
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    _git(folder, "init", "-q", "-b", "main")
    _git(folder, "config", "user.name", "Dev")
    _git(folder, "config", "user.email", "dev@example.com")
    return folder


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:secret-token@github.com/acme/app.git", "https://github.com/acme/app.git"),
        ("https://token@gitlab.example.com/acme/app", "https://gitlab.example.com/acme/app"),
        ("ssh://git@host:2222/acme/app.git", "ssh://host:2222/acme/app.git"),
        ("https://github.com/acme/app.git", "https://github.com/acme/app.git"),
        ("git@github.com:acme/app.git", "git@github.com:acme/app.git"),  # no password possible
        ("  /srv/git/app.git \n", "/srv/git/app.git"),
    ],
)
def test_credentials_are_removed_from_a_remote(url: str, expected: str) -> None:
    assert strip_credentials(url) == expected


def test_the_remote_and_identity_of_a_repository_are_read(repo: Path) -> None:
    _git(repo, "remote", "add", "origin", "https://user:secret-token@example.com/acme/app.git")
    facts = read_git_facts(repo)
    assert facts == GitFacts("https://example.com/acme/app.git", "Dev", "dev@example.com")
    assert "secret-token" not in repr(facts)


def test_a_repository_without_origin_has_no_remote(repo: Path) -> None:
    assert read_git_facts(repo).remote is None


def test_a_missing_folder_gives_unknown_facts_without_failing(tmp_path: Path) -> None:
    facts = read_git_facts(tmp_path / "does-not-exist")
    assert facts.remote is None


def test_head_is_read_and_is_none_before_the_first_commit(repo: Path) -> None:
    assert read_head(repo) is None
    (repo / "a.txt").write_text("a", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    assert read_head(repo) == _git(repo, "rev-parse", "HEAD")


def test_no_git_program_gives_unknown_facts(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    assert read_git_facts(repo) == GitFacts()
    assert read_head(repo) is None
