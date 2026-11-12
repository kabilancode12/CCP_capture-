"""Snapshot behaviour beyond the ported proof-of-concept checks."""

import sys
from pathlib import Path

import pytest

from ccp.capture.snapshots.git import Git
from ccp.capture.snapshots.service import SnapshotService
from ccp.capture.snapshots.taker import SnapshotTaker, _safe_name, file_has_secret
from ccp.capture.state import StateStore

from .conftest import NO_CONFIG, World, git, init_bare, init_repo, make_service, ok

SID = "sess-x"


def _fake_key() -> str:
    return "sk_" + "live_" + "0123456789abcdef"  # invented; built from pieces


def _write(world: World, name: str, text: str) -> None:
    (world.repo / name).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- starting a session


def test_a_clean_repository_gets_no_baseline_snapshot(world: World) -> None:
    outcome = world.service().start(SID, world.repo)
    assert outcome is not None and "no snapshot (no-changes)" in outcome
    assert world.snapshot_refs(world.repo) == [
        "refs/claude-capture/users/dev_example.com/heads/main"
    ]  # only the branch backup


def test_uncommitted_work_present_at_the_start_becomes_the_baseline(world: World) -> None:
    _write(world, "app.py", 'print("work in progress")\n')
    outcome = world.service().start(SID, world.repo)
    assert outcome is not None and "snapshot 0001" in outcome
    message = git(
        world.capture, "log", "-1", "--format=%B", f"refs/claude-capture/sessions/{SID}/0001"
    )
    assert "event: SessionStart" in message  # the indexer treats this one as the starting state


def test_session_start_runs_again_after_compact_without_a_new_snapshot(world: World) -> None:
    service = world.service()
    _write(world, "app.py", "changed\n")
    service.start(SID, world.repo)
    again = service.start(SID, world.repo)  # SessionStart fires again after /compact
    assert again is not None and "no snapshot (unchanged)" in again


def test_branch_backups_follow_the_branch_and_handle_slashes(world: World) -> None:
    git(world.repo, "branch", "feature/login")
    service = world.service()
    service.start(SID, world.repo)

    base = "refs/claude-capture/users/dev_example.com/heads"
    refs = world.snapshot_refs()
    assert f"{base}/main" in refs and f"{base}/feature/login" in refs

    _write(world, "app.py", "moves main\n")
    git(world.repo, "commit", "-qam", "moves main")  # the branch tip moves: the backup follows
    service.finish(SID, world.repo)
    assert git(world.capture, "rev-parse", f"{base}/main") == git(world.repo, "rev-parse", "main")


# ---------------------------------------------------------------- what is included and excluded


def test_a_deleted_file_is_absent_from_the_next_snapshot(world: World) -> None:
    service = world.service()
    _write(world, "notes.md", "first\n")
    service.snapshot(SID, world.repo, "Stop")
    (world.repo / "notes.md").unlink()
    service.snapshot(SID, world.repo, "Stop")

    first, second = (f"refs/claude-capture/sessions/{SID}/000{n}" for n in (1, 2))
    assert ok(
        world.capture, "cat-file", "-e", f"{first}:notes.md"
    )  # earlier snapshots still have it
    assert not ok(world.capture, "cat-file", "-e", f"{second}:notes.md")


def test_a_binary_file_is_included_and_not_scanned_for_secrets(world: World) -> None:
    (world.repo / "image.bin").write_bytes(b"\x00\x01\x02 " + _fake_key().encode() + b"\x00")
    world.service().snapshot(SID, world.repo, "Stop")
    assert ok(world.capture, "cat-file", "-e", f"refs/claude-capture/sessions/{SID}/0001:image.bin")


def test_a_file_with_a_secret_is_reset_to_its_committed_version(world: World) -> None:
    _write(
        world, "app.py", f'KEY = "{_fake_key()}"\nprint("v2")\n'
    )  # a tracked file gains a secret
    _write(world, "notes.md", "another change, so that a snapshot is taken\n")
    world.service().snapshot(SID, world.repo, "Stop")
    in_snapshot = git(world.capture, "show", f"refs/claude-capture/sessions/{SID}/0001:app.py")
    assert in_snapshot == 'print("v1")'  # the committed version, not the edit with the secret


def test_the_secret_is_never_stored_anywhere_in_the_capture_project(world: World) -> None:
    _write(world, "config.py", f'API_KEY = "{_fake_key()}"\n')
    world.service().snapshot(SID, world.repo, "Stop")
    everything = git(
        world.capture,
        "grep",
        "-I",
        "-l",
        _fake_key(),
        "refs/claude-capture/sessions/sess-x/0001",
        check=False,
    )
    assert everything == ""


def test_names_are_made_safe_for_the_commit_message() -> None:
    # Platform independent: a name with line breaks must not be able to forge message lines.
    assert _safe_name("evil\nsession: forged\nseq: 99") == "evil?session: forged?seq: 99"
    assert _safe_name("tab\there\rcarriage") == "tab?here?carriage"
    assert _safe_name("plain name.py") == "plain name.py"


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows does not allow line breaks or colons in file names"
)
def test_a_hostile_file_name_cannot_inject_lines_into_the_commit_message(world: World) -> None:
    # A file name may contain a newline; it must not be able to forge "session:" or "seq:" lines.
    name = "evil\nsession: forged\nseq: 99"
    (world.repo / name).write_text(f'KEY = "{_fake_key()}"\n', encoding="utf-8")
    _write(world, "notes.md", "another change, so that a snapshot is taken\n")
    world.service().snapshot(SID, world.repo, "Stop")
    message = git(
        world.capture, "log", "-1", "--format=%B", f"refs/claude-capture/sessions/{SID}/0001"
    )
    assert "session: forged" not in message.splitlines()
    assert message.count("session:") == 1
    assert "excluded: evil?session: forged?seq: 99" in message


def test_an_excluded_file_is_mentioned_in_the_log_even_when_no_snapshot_results(
    world: World,
) -> None:
    # The only change is a file with a secret: it is excluded, the tree equals the committed one, so
    # there is nothing to record. The outcome still says that a file was left out.
    _write(world, "config.py", f'API_KEY = "{_fake_key()}"\n')
    outcome = world.service().snapshot(SID, world.repo, "Stop")
    assert outcome == "no snapshot (no-changes) (1 file(s) excluded: secret)"


def test_file_has_secret_cases(tmp_path: Path) -> None:
    secret, clean, binary = tmp_path / "s.txt", tmp_path / "c.txt", tmp_path / "b.bin"
    secret.write_text(f"token = {_fake_key()}\n", encoding="utf-8")
    clean.write_text("nothing here\n", encoding="utf-8")
    binary.write_bytes(b"\x00" + _fake_key().encode())
    assert file_has_secret(secret) is True
    assert file_has_secret(clean) is False
    assert file_has_secret(binary) is False  # binary files are not scanned
    assert file_has_secret(tmp_path / "missing.txt") is False  # vanished while we looked


def test_a_text_file_too_large_to_scan_is_excluded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ccp.capture.snapshots.taker.SECRET_SCAN_LIMIT_BYTES", 100)
    _write(world, "big.txt", "x" * 500)  # fail closed: it cannot be checked, so it is left out
    _write(world, "notes.md", "another change, so that a snapshot is taken\n")
    world.service().snapshot(SID, world.repo, "Stop")
    ref = f"refs/claude-capture/sessions/{SID}/0001"
    assert not ok(world.capture, "cat-file", "-e", f"{ref}:big.txt")
    assert "excluded: big.txt" in git(world.capture, "log", "-1", "--format=%B", ref)


# ---------------------------------------------------------------- unpushed commits and secrets


def test_a_secret_in_an_unpushed_commit_keeps_the_snapshot_local(world: World) -> None:
    _write(world, "config.py", f'KEY = "{_fake_key()}"\n')
    git(world.repo, "add", "-A")
    git(world.repo, "commit", "-qm", "oops, committed a secret locally")  # not pushed anywhere
    _write(world, "app.py", 'print("v2")\n')

    outcome = world.service().snapshot(SID, world.repo, "Stop")

    assert outcome is not None and "kept local" in outcome
    assert world.snapshot_refs(world.repo)  # the snapshot exists in the developer's repository...
    assert world.snapshot_refs() == []  # ...but nothing reached the capture project
    assert not ok(world.capture, "cat-file", "-e", git(world.repo, "rev-parse", "HEAD"))


def test_the_unpushed_commit_scan_runs_again_only_when_head_moves(world: World) -> None:
    service = world.service()
    _write(world, "app.py", "one\n")
    service.snapshot(SID, world.repo, "Stop")
    marker = StateStore(world.state_dir).get_marker(SID, "snapshot-scanned-head")
    assert marker == world.start  # the clean scan was remembered for this HEAD

    _write(world, "app.py", "two\n")
    service.snapshot(SID, world.repo, "Stop")  # HEAD did not move: no new scan needed
    assert StateStore(world.state_dir).get_marker(SID, "snapshot-scanned-head") == world.start


# ---------------------------------------------------------------- unusual repositories


def test_an_empty_repository_gets_a_root_snapshot(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "empty")
    capture = init_bare(tmp_path / "capture.git")
    (repo / "first.py").write_text("print(1)\n", encoding="utf-8")
    outcome = make_service(tmp_path / "state", remote=capture).snapshot(SID, repo, "Stop")

    assert outcome is not None and "snapshot 0001" in outcome
    ref = f"refs/claude-capture/sessions/{SID}/0001"
    assert git(capture, "show", f"{ref}:first.py") == "print(1)"
    assert git(capture, "rev-list", "--count", ref) == "1"  # a root commit: there was no HEAD


def test_a_detached_head_is_recorded_as_such(world: World) -> None:
    git(world.repo, "checkout", "-q", "--detach")
    _write(world, "app.py", "x\n")
    world.service().snapshot(SID, world.repo, "Stop")
    message = git(
        world.capture, "log", "-1", "--format=%B", f"refs/claude-capture/sessions/{SID}/0001"
    )
    assert "branch: detached" in message


def test_a_developer_without_a_git_identity_still_gets_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_config = tmp_path / "empty-gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    for key, value in NO_CONFIG.items():
        monkeypatch.setenv(key, value)
    repo = init_repo(tmp_path / "repo", identity=False)
    git(
        repo,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    (repo / "a.py").write_text("1\n", encoding="utf-8")

    outcome = make_service(tmp_path / "state").snapshot(SID, repo, "Stop")

    assert outcome is not None and "snapshot 0001" in outcome
    ref = f"refs/claude-capture/sessions/{SID}/0001"
    assert git(repo, "log", "-1", "--format=%an <%ae>", ref) == "claude-capture <capture@localhost>"


def test_sequence_numbers_continue_across_separate_processes(world: World) -> None:
    _write(world, "app.py", "one\n")
    world.service().snapshot(SID, world.repo, "Stop")
    _write(world, "app.py", "two\n")
    world.service().snapshot(SID, world.repo, "Stop")  # a new service object, as a new hook process
    refs = [r for r in world.snapshot_refs() if "/sessions/" in r]
    assert refs == [
        f"refs/claude-capture/sessions/{SID}/0001",
        f"refs/claude-capture/sessions/{SID}/0002",
    ]


def test_different_sessions_are_numbered_independently(world: World) -> None:
    service = world.service()
    _write(world, "app.py", "one\n")
    service.snapshot("sess-a", world.repo, "Stop")
    service.snapshot(
        "sess-b", world.repo, "Stop"
    )  # same tree, but another session: its own snapshot
    refs = world.snapshot_refs()
    assert "refs/claude-capture/sessions/sess-a/0001" in refs
    assert "refs/claude-capture/sessions/sess-b/0001" in refs


# ---------------------------------------------------------------- safety


def test_the_developers_index_branches_and_files_are_never_modified(world: World) -> None:
    _write(world, "staged.txt", "staged by the developer\n")
    git(world.repo, "add", "staged.txt")
    _write(world, "app.py", "edited\n")
    # `git status` itself refreshes the index, so run it first; the bytes are recorded afterwards.
    status = git(world.repo, "status", "--porcelain")
    before = {
        "index": (world.repo / ".git" / "index").read_bytes(),
        "branches": git(
            world.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"
        ),
        "head": (world.repo / ".git" / "HEAD").read_text(encoding="utf-8"),
        "status": status,
    }

    world.service().start(SID, world.repo)
    world.service().snapshot(SID, world.repo, "Stop")
    world.service().finish(SID, world.repo)

    assert (world.repo / ".git" / "index").read_bytes() == before["index"]  # byte for byte
    assert (
        git(world.repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
        == before["branches"]
    )
    assert (world.repo / ".git" / "HEAD").read_text(encoding="utf-8") == before["head"]
    assert git(world.repo, "status", "--porcelain") == before["status"]


def test_no_temporary_files_are_left_behind(world: World) -> None:
    _write(world, "app.py", "edited\n")
    world.service().snapshot(SID, world.repo, "Stop")
    work_folder = world.state_dir / "snapshot-work"
    assert not work_folder.exists() or list(work_folder.iterdir()) == []
    assert not list((world.repo / ".git").glob("index.lock"))


def test_a_disabled_service_does_nothing_at_all(world: World) -> None:
    service = world.service(enabled=False)
    _write(world, "app.py", "edited\n")
    assert service.start(SID, world.repo) is None
    assert service.snapshot(SID, world.repo, "Stop") is None
    assert service.finish(SID, world.repo) is None
    assert world.snapshot_refs(world.repo) == []


def test_a_folder_that_does_not_exist_is_not_a_repository(world: World, tmp_path: Path) -> None:
    assert world.service().snapshot(SID, tmp_path / "nowhere", "Stop") == "not a git repository"


def test_an_unsafe_session_id_is_refused_without_an_exception(world: World) -> None:
    outcome = world.service().snapshot("../evil", world.repo, "Stop")
    assert outcome is not None and outcome.startswith("failed")
    assert world.snapshot_refs(world.repo) == []


def test_without_a_capture_project_snapshots_stay_in_the_local_repository(world: World) -> None:
    service = SnapshotService(
        StateStore(world.state_dir), world.state_dir, enabled=True, remote=None, remote_base=None
    )
    _write(world, "app.py", "edited\n")
    outcome = service.snapshot(SID, world.repo, "Stop")
    assert outcome is not None and "kept locally (no capture project configured)" in outcome
    assert world.snapshot_refs(world.repo) == [f"refs/claude-capture/sessions/{SID}/0001"]


def test_the_taker_can_be_used_directly(world: World, tmp_path: Path) -> None:
    taker = SnapshotTaker(Git(world.repo), StateStore(tmp_path / "s"), tmp_path / "w")
    _write(world, "app.py", "edited\n")
    result = taker.take(SID, "Stop")
    assert result.status == "created" and result.ref is not None and result.commit is not None
    assert taker.take(SID, "Stop").status == "unchanged"
