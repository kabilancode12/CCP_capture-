"""Code snapshots through the hooks: real hook processes, a real repository, a real capture project.

A scripted session: Claude Code starts, edits files, answers, and exits. Each hook runs as its own
process with the real JSON payload, against the real API. The checks are on what the developer's
repository still looks like (untouched) and what the capture project received.
"""

import subprocess
import time
from pathlib import Path

import pytest

from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore

from .conftest import RunningApi
from .test_hooks_end_to_end import (
    SESSION,
    Machine,
    _assert_quiet_success,
    _stored,
    _wait_until,
    _write,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _git(folder: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603  (fixed program, list of arguments)
        ["git", *args],  # noqa: S607
        cwd=folder,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def _refs(folder: Path) -> list[str]:
    return _git(folder, "for-each-ref", "--format=%(refname)", "refs/claude-capture").splitlines()


def _snapshots(folder: Path) -> list[str]:
    return [r for r in _refs(folder) if "/sessions/" in r]


@pytest.fixture
def project(tmp_path: Path, api: RunningApi) -> tuple[Machine, Path, Path]:
    """A machine with snapshots switched on, a repository to work in, and a capture project."""
    machine = Machine(tmp_path, api.url)
    capture = tmp_path / "capture.git"
    capture.mkdir()
    _git(capture, "init", "-q", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    (repo / "app.py").write_text('print("v1")\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    machine.extra_env["CCP_SNAPSHOT_REMOTE"] = capture.as_posix()
    return machine, repo, capture


def test_a_session_captures_code_and_conversation_without_touching_the_repository(
    project: tuple[Machine, Path, Path],
    object_store: ObjectStore,
    database: DatabaseSettings,
) -> None:
    machine, repo, capture = project
    head = _git(repo, "rev-parse", "HEAD")
    cwd = str(repo)

    # SessionStart: clean repository, so no baseline snapshot; the branch is backed up.
    _assert_quiet_success(machine.hook("SessionStart", cwd=cwd, source="startup"))
    assert _snapshots(capture) == []
    assert any(r.endswith("/heads/main") for r in _refs(capture))

    # Claude edits a file; the developer had staged another file of their own.
    (repo / "notes.md").write_text("staged by the developer\n", encoding="utf-8")
    _git(repo, "add", "notes.md")
    (repo / "app.py").write_text('print("v2 - edited by Claude")\n', encoding="utf-8")
    _write(machine.transcript, {"type": "user", "version": "2.1.289", "message": "edit app.py"})
    staged_before = _git(repo, "diff", "--cached", "--name-only")

    _assert_quiet_success(machine.hook("Stop", cwd=cwd))

    # The conversation went to the API and the code to the capture project...
    assert '"message":"edit app.py"' in _stored(object_store)
    assert _snapshots(capture) == [f"refs/claude-capture/sessions/{SESSION}/0001"]
    assert "v2 - edited by Claude" in _git(capture, "show", f"{_snapshots(capture)[0]}:app.py")
    # ...and the developer's repository is exactly as they left it.
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "diff", "--cached", "--name-only") == staged_before
    assert "v2" in (repo / "app.py").read_text(encoding="utf-8")
    assert _git(repo, "branch", "--format=%(refname:short)") == "main"

    # A response that changes nothing creates no new snapshot.
    _assert_quiet_success(machine.hook("Stop", cwd=cwd))
    assert len(_snapshots(capture)) == 1


def test_session_end_takes_the_final_snapshot_in_the_background(
    project: tuple[Machine, Path, Path], object_store: ObjectStore
) -> None:
    machine, repo, capture = project
    cwd = str(repo)
    _write(machine.transcript, {"type": "user", "uuid": "u1"})
    _assert_quiet_success(machine.hook("Stop", cwd=cwd))  # nothing changed in the code yet

    (repo / "app.py").write_text('print("last edit")\n', encoding="utf-8")
    _write(machine.transcript, {"type": "assistant", "uuid": "a-final"}, append=True)
    started = time.monotonic()
    _assert_quiet_success(machine.hook("SessionEnd", cwd=cwd, reason="prompt_input_exit"))
    assert time.monotonic() - started < 15  # the hook did not wait for Git or the upload

    # The detached flush uploads the last lines AND snapshots the last edit.
    assert _wait_until(lambda: '"uuid":"a-final"' in _stored(object_store))
    assert _wait_until(lambda: len(_snapshots(capture)) == 1), "the final snapshot did not arrive"
    assert "last edit" in _git(capture, "show", f"{_snapshots(capture)[0]}:app.py")


def test_the_hook_log_describes_the_code_step_without_content_or_names(
    project: tuple[Machine, Path, Path],
) -> None:
    machine, repo, _ = project
    fake_key = "sk_" + "live_" + "0123456789abcdef"  # invented; built from pieces
    (repo / "config.py").write_text(f'API_KEY = "{fake_key}"\n', encoding="utf-8")
    (repo / "app.py").write_text('print("v2")\n', encoding="utf-8")
    _write(machine.transcript, {"type": "user", "uuid": "u1"})

    _assert_quiet_success(machine.hook("Stop", cwd=str(repo)))

    log = machine.log()
    assert "code: snapshot 0001 (1 file(s) excluded: secret), pushed" in log
    assert "config.py" not in log and fake_key not in log  # the log says how many, never which


def test_a_failed_push_is_retried_at_the_next_session_start(
    project: tuple[Machine, Path, Path], tmp_path: Path
) -> None:
    machine, repo, capture = project
    cwd = str(repo)
    working_remote = machine.extra_env["CCP_SNAPSHOT_REMOTE"]
    machine.extra_env["CCP_SNAPSHOT_REMOTE"] = (tmp_path / "not-created-yet.git").as_posix()

    (repo / "app.py").write_text('print("v2")\n', encoding="utf-8")
    _write(machine.transcript, {"type": "user", "uuid": "u1"})
    _assert_quiet_success(machine.hook("Stop", cwd=cwd))  # the push fails: queued, not an error
    assert _snapshots(capture) == []
    assert "queued for the next session start" in machine.log()

    machine.extra_env["CCP_SNAPSHOT_REMOTE"] = working_remote  # the capture project is reachable
    _assert_quiet_success(machine.hook("SessionStart", cwd=cwd, source="resume"))
    assert _snapshots(capture) == [f"refs/claude-capture/sessions/{SESSION}/0001"]


def test_snapshots_are_off_unless_configured(
    project: tuple[Machine, Path, Path],
) -> None:
    machine, repo, capture = project
    del machine.extra_env["CCP_SNAPSHOT_REMOTE"]  # nothing switches them on

    (repo / "app.py").write_text('print("v2")\n', encoding="utf-8")
    _write(machine.transcript, {"type": "user", "uuid": "u1"})
    _assert_quiet_success(machine.hook("Stop", cwd=str(repo)))

    assert _refs(repo) == [] and _refs(capture) == []  # no hidden refs anywhere
    assert "code:" not in machine.log()
