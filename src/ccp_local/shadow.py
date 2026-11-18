"""Shadow snapshots: a separate local Git repo that records the project after every change.

The developer's own repo is never touched: we point Git at `shadow.git` (GIT_DIR) with our own
index file and use the project folder only as the work tree. Each snapshot is one commit on
`main` whose diff to its parent is exactly what changed since the previous snapshot, so:
  - snapshot at UserPromptSubmit  -> what the human changed between turns (author "human")
  - snapshot after each tool call -> what that Claude tool changed       (author "claude")
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

# Never snapshot these (on top of the project's own .gitignore).
EXCLUDES = """node_modules/
.venv/
venv/
__pycache__/
*.pyc
dist/
build/
target/
bin/Debug/
obj/
.idea/
.DS_Store
"""
MAX_FILES_LISTED = 200


def _git(env: dict, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], env=env, capture_output=True, text=True, timeout=60)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {out.stderr.strip()}")
    return out.stdout.strip() if out.returncode == 0 else ""


def _env(gitdir: Path, root: Path, author: str) -> dict:
    env = dict(os.environ)
    for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(key, None)
    env.update(
        GIT_DIR=str(gitdir),
        GIT_WORK_TREE=str(root),
        GIT_INDEX_FILE=str(gitdir / "ccp-index"),
        GIT_AUTHOR_NAME=author,
        GIT_AUTHOR_EMAIL=f"{author}@ccp.local",
        GIT_COMMITTER_NAME="ccp",
        GIT_COMMITTER_EMAIL="ccp@ccp.local",
    )
    return env


def _init(gitdir: Path) -> None:
    if (gitdir / "HEAD").exists():
        return
    subprocess.run(["git", "init", "--bare", "-q", str(gitdir)], check=True, timeout=30)
    git = ["git", "--git-dir", str(gitdir)]
    subprocess.run([*git, "symbolic-ref", "HEAD", "refs/heads/main"], timeout=10)
    for key, value in (("core.autocrlf", "false"), ("core.quotepath", "off"), ("gc.auto", "0")):
        subprocess.run([*git, "config", key, value], timeout=10)
    (gitdir / "info").mkdir(exist_ok=True)
    (gitdir / "info" / "exclude").write_text(EXCLUDES, encoding="utf-8")


class _Lock:
    """Cross-platform lock file so two sessions of one project never commit at the same time."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> _Lock:
        deadline = time.time() + 10
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 60:  # left by a killed hook
                        self.path.unlink()
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise TimeoutError("shadow repo busy") from None
                time.sleep(0.05)

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


def snapshot(pdir: Path, root: Path, author: str, title: str, trailers: dict) -> dict | None:
    """Commit the current state of `root`; return {commit, parent, files} or None if unchanged."""
    gitdir = pdir / "shadow.git"
    _init(gitdir)
    env = _env(gitdir, root, author)
    with _Lock(gitdir / "ccp.lock"):
        _git(env, "add", "-A", "--ignore-errors", ".", check=False)
        tree = _git(env, "write-tree")
        parent = _git(env, "rev-parse", "-q", "--verify", "refs/heads/main", check=False)
        if parent and _git(env, "rev-parse", f"{parent}^{{tree}}") == tree:
            return None
        body = "\n".join(f"CCP-{k}: {v}" for k, v in trailers.items() if v)
        cmd = ["commit-tree", tree, "-m", title, "-m", body]
        commit = _git(env, *(cmd + (["-p", parent] if parent else [])))
        _git(env, "update-ref", "refs/heads/main", commit, *([parent] if parent else []))
    diff = ["diff-tree", "-r", "--no-commit-id", "--name-status"]
    lines = _git(env, *(diff + ([parent, commit] if parent else ["--root", commit]))).splitlines()
    files = [line.replace("\t", " ", 1) for line in lines[:MAX_FILES_LISTED]]
    return {"commit": commit, "parent": parent or None, "files": files, "file_count": len(lines)}
