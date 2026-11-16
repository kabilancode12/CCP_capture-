"""Helpers for the snapshot tests: real Git repositories in temporary folders (no network)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ccp.capture.snapshots.reports import SnapshotReporter
from ccp.capture.snapshots.service import SnapshotService
from ccp.capture.state import StateStore

NO_CONFIG = {"GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    """Run Git in ``cwd`` and return its output (the way a developer or a test would)."""
    result = subprocess.run(  # noqa: S603  (fixed program, list of arguments)
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, **(env or {})},
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def ok(cwd: Path, *args: str) -> bool:
    """Return whether a Git command succeeds."""
    return (
        subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def init_repo(path: Path, *, identity: bool = True) -> Path:
    """Create a repository on branch ``main``, with the developer's identity unless told not to."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    if identity:
        git(path, "config", "user.email", "dev@example.com")
        git(path, "config", "user.name", "Dev")
    git(path, "config", "core.autocrlf", "false")
    return path


def init_bare(path: Path) -> Path:
    """Create an empty bare repository (a stand-in for a server)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "--bare")
    return path


def make_service(
    state_dir: Path,
    *,
    remote: Path | str | None = None,
    remote_base: Path | str | None = None,
    enabled: bool = True,
    reporter: SnapshotReporter | None = None,
) -> SnapshotService:
    """Create a snapshot service that pushes to ``remote`` (a path or URL) if given."""
    return SnapshotService(
        StateStore(state_dir),
        state_dir,
        enabled=enabled,
        remote=_as_url(remote),
        remote_base=_as_url(remote_base),
        git_timeout_seconds=30,
        push_timeout_seconds=30,
        reporter=reporter,
    )


def _as_url(value: Path | str | None) -> str | None:
    return value.as_posix() if isinstance(value, Path) else value


@pytest.fixture(scope="session")
def world_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The repositories of a :class:`World`, built once (running Git is slow on Windows)."""
    root = tmp_path_factory.mktemp("world-template")
    init_bare(root / "main.git")
    init_bare(root / "capture.git")
    repo = init_repo(root / "repo")
    (repo / ".gitignore").write_text(".env\n*.key\n", encoding="utf-8")
    (repo / "app.py").write_text('print("v1")\n', encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "initial")
    git(repo, "remote", "add", "origin", (root / "main.git").as_posix())
    git(repo, "push", "-q", "origin", "main")
    return root


@pytest.fixture
def world(tmp_path: Path, world_template: Path) -> "World":
    """A developer repository with a pushed main branch, a capture project and a state folder."""
    return World(tmp_path, world_template)


class World:
    """A repository, its main remote, a capture project and a state folder, in one place."""

    def __init__(self, root: Path, template: Path) -> None:
        shutil.copytree(template, root, dirs_exist_ok=True)  # a copy is much faster than rebuilding
        self.root = root
        self.main = root / "main.git"
        self.capture = root / "capture.git"
        self.repo = root / "repo"
        self.state_dir = root / "state"
        git(
            self.repo, "remote", "set-url", "origin", self.main.as_posix()
        )  # the copy lives elsewhere
        self.start = git(self.repo, "rev-parse", "HEAD")

    def service(self, **options: object) -> SnapshotService:
        options.setdefault("remote", self.capture)
        return make_service(self.state_dir, **options)  # type: ignore[arg-type]

    def snapshot_refs(self, where: Path | None = None) -> list[str]:
        """Names of the hidden snapshot refs in ``where`` (default: the capture project)."""
        refs = git(
            where or self.capture, "for-each-ref", "--format=%(refname)", "refs/claude-capture"
        )
        return refs.splitlines()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this folder as slow: each one starts dozens of Git processes."""
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.slow)
