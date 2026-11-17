"""The old proof of concept's end-to-end checks (reference/poc/test_poc.sh), ported.

The same scenario, in the same order, with each check as its own test. 27 checks existed; 23 apply
here. Four do not, by design:

* "marker returned as additional context": the SessionStart hook must print nothing now;
* "transcript uploaded", "non-repository transcript still uploaded" and "late transcript lines
  caught up": transcripts are uploaded by the capture client (see test_client.py and the
  integration tests), not by the snapshot code.
"""

from pathlib import Path

import pytest

from .conftest import git, init_bare, init_repo, make_service, ok

SID = "sess-1111"
S1 = f"refs/claude-capture/sessions/{SID}/0001"
S2 = f"refs/claude-capture/sessions/{SID}/0002"
S3 = f"refs/claude-capture/sessions/{SID}/0003"
S4 = f"refs/claude-capture/sessions/{SID}/0004"


class Poc:
    """Runs the whole scenario once and keeps what the checks need."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.main = init_bare(root / "main.git")
        self.capture = init_bare(root / "capture.git")
        self.repo = init_repo(root / "repo")
        self.state = root / "state"
        repo = self.repo

        (repo / ".gitignore").write_text(".env\n*.key\n", encoding="utf-8")
        (repo / "app.py").write_text('print("v1")\n', encoding="utf-8")
        (repo / "staged.txt").write_text("staged base\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "initial")
        git(repo, "remote", "add", "origin", self.main.as_posix())
        git(repo, "push", "-q", "origin", "main")
        self.start = git(repo, "rev-parse", "HEAD")
        service = make_service(self.state, remote=self.capture)

        # 1. Session start
        self.start_outcome = service.start(SID, repo)

        # 2. Claude edits files; the developer has staged something of their own
        (repo / "staged.txt").write_text("staged change by developer\n", encoding="utf-8")
        git(repo, "add", "staged.txt")
        (repo / "app.py").write_text('print("v2 - edited by Claude")\n', encoding="utf-8")
        (repo / "notes.md").write_text("# notes\n", encoding="utf-8")
        (repo / ".env").write_text("AWS_SECRET=abc\n", encoding="utf-8")
        fake_key = "sk_" + "live_" + "0123456789abcdef"  # invented; built from pieces
        (repo / "config.py").write_text(f'API_KEY = "{fake_key}"\n', encoding="utf-8")
        self.staged_before = git(repo, "diff", "--cached", "--name-only")
        self.outcome_1 = service.snapshot(SID, repo, "Stop")
        # What the developer sees right after the snapshot: it must not have changed anything.
        self.head_after_1 = git(repo, "rev-parse", "HEAD")
        self.staged_after_1 = git(repo, "diff", "--cached", "--name-only")
        self.app_after_1 = (repo / "app.py").read_text(encoding="utf-8")
        self.files_after_1 = {
            name: (repo / name).is_file() for name in ("config.py", "notes.md", ".env")
        }

        # 3. Claude responds without changing files
        self.outcome_3 = service.snapshot(SID, repo, "Stop")
        self.refs_after_3 = git(
            self.capture, "for-each-ref", "--format=%(refname)", "refs/claude-capture/sessions"
        )

        # 4. Claude edits again
        (repo / "app.py").write_text('print("v3")\n', encoding="utf-8")
        service.snapshot(SID, repo, "Stop")

        # 5. Developer commits locally but does not push
        (repo / "config.py").unlink()
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "local work, not pushed")
        self.local = git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text('print("v4")\n', encoding="utf-8")
        service.snapshot(SID, repo, "Stop")

        # 6. Capture project unreachable, then back
        (repo / "app.py").write_text('print("v5")\n', encoding="utf-8")
        broken = make_service(self.state, remote=root / "does-not-exist.git")
        self.outcome_6 = broken.snapshot(SID, repo, "Stop")
        self.queue_text = (self.state / "pending-pushes.jsonl").read_text(encoding="utf-8")
        service.start("sess-2222", repo)

        # 6c. Capture project derived from the base address: <base>/<repository name>-capture.git
        self.derived = init_bare(root / "main-capture.git")  # the origin is main.git -> "main"
        (repo / "app.py").write_text('print("base")\n', encoding="utf-8")
        make_service(self.state, remote_base=root).snapshot("sess-5555", repo, "Stop")

        # 7. Session end, and a session outside any repository
        (repo / "app.py").write_text('print("v6")\n', encoding="utf-8")
        service.finish(SID, repo)
        not_a_repo = root / "not-a-repo"
        not_a_repo.mkdir()
        self.non_repo_outcome = service.snapshot("sess-3333", not_a_repo, "Stop")

    def capture_git(self, *args: str, check: bool = True) -> str:
        return git(self.capture, *args, check=check)

    def capture_has(self, ref: str) -> bool:
        return ok(self.capture, "rev-parse", "-q", "--verify", ref)


@pytest.fixture(scope="module")
def poc(tmp_path_factory: pytest.TempPathFactory) -> Poc:
    return Poc(tmp_path_factory.mktemp("poc"))


# ---------------------------------------------------------------- 1. session start


def test_branch_backup_is_pushed_to_the_capture_project(poc: Poc) -> None:
    assert poc.capture_has("refs/claude-capture/users/dev_example.com/heads/main")


# ---------------------------------------------------------------- 2. Claude edits files


def test_snapshot_1_is_pushed_to_the_capture_project(poc: Poc) -> None:
    assert poc.capture_has(S1)


def test_the_developers_head_is_unchanged(poc: Poc) -> None:
    assert poc.head_after_1 == poc.start


def test_the_developers_staged_changes_are_untouched(poc: Poc) -> None:
    assert poc.staged_before == "staged.txt"  # the developer really had something staged
    assert poc.staged_after_1 == poc.staged_before


def test_the_developers_working_files_are_untouched(poc: Poc) -> None:
    assert "v2" in poc.app_after_1  # Claude's edit is still on disk
    assert poc.files_after_1 == {"config.py": True, "notes.md": True, ".env": True}  # none removed


def test_no_new_branch_is_created(poc: Poc) -> None:
    assert git(poc.repo, "branch", "--format=%(refname:short)") == "main"


def test_the_snapshot_contains_the_uncommitted_edit(poc: Poc) -> None:
    assert "v2" in poc.capture_git("show", f"{S1}:app.py")


def test_the_snapshot_contains_a_new_untracked_file(poc: Poc) -> None:
    assert poc.capture_git("show", f"{S1}:notes.md") == "# notes"


def test_an_ignored_file_is_excluded(poc: Poc) -> None:
    assert not ok(poc.capture, "cat-file", "-e", f"{S1}:.env")


def test_a_file_with_a_secret_is_excluded(poc: Poc) -> None:
    assert not ok(poc.capture, "cat-file", "-e", f"{S1}:config.py")
    assert poc.files_after_1["config.py"]  # it was on disk (and is still the developer's file)


def test_the_exclusion_is_recorded_in_the_snapshot_message(poc: Poc) -> None:
    assert "excluded: config.py" in poc.capture_git("log", "-1", "--format=%B", S1)


def test_the_snapshot_builds_on_the_developers_commit(poc: Poc) -> None:
    assert poc.capture_git("rev-parse", f"{S1}^") == poc.start


# ---------------------------------------------------------------- 3. no change


def test_a_response_without_changes_creates_no_duplicate_snapshot(poc: Poc) -> None:
    assert poc.outcome_3 is not None and poc.outcome_3.startswith("no snapshot (unchanged)")
    assert poc.refs_after_3.splitlines() == [S1]  # still only snapshot 1, nothing new


# ---------------------------------------------------------------- 4. Claude edits again


def test_snapshot_2_is_pushed(poc: Poc) -> None:
    assert poc.capture_has(S2)


def test_the_difference_between_snapshots_is_only_the_new_edit(poc: Poc) -> None:
    assert poc.capture_git("diff", "--name-only", S1, S2) == "app.py"


# ---------------------------------------------------------------- 5. local commit, not pushed


def test_snapshot_3_builds_on_the_unpushed_commit(poc: Poc) -> None:
    assert poc.capture_git("rev-parse", f"{S3}^") == poc.local


def test_the_unpushed_commit_is_now_preserved_in_the_capture_project(poc: Poc) -> None:
    assert ok(poc.capture, "cat-file", "-e", poc.local)


def test_the_unpushed_commit_is_still_not_in_the_main_repository(poc: Poc) -> None:
    assert not ok(poc.main, "cat-file", "-e", poc.local)


# ---------------------------------------------------------------- 6. capture project unreachable


def test_a_failed_push_is_queued_and_the_session_is_not_blocked(poc: Poc) -> None:
    assert poc.outcome_6 is not None and "queued" in poc.outcome_6
    assert S4 in poc.queue_text


def test_the_queued_snapshot_is_delivered_at_the_next_session_start(poc: Poc) -> None:
    assert poc.capture_has(S4)


# ---------------------------------------------------------------- 6c, 7


def test_the_capture_project_can_be_derived_from_the_base_address(poc: Poc) -> None:
    refs = git(poc.derived, "for-each-ref", "--format=%(refname)", "refs/claude-capture")
    assert "refs/claude-capture/sessions/sess-5555/0001" in refs.splitlines()


def test_the_final_snapshot_is_pushed_at_session_end(poc: Poc) -> None:
    assert poc.capture_has(f"refs/claude-capture/sessions/{SID}/0005")


def test_a_session_outside_any_repository_ends_cleanly(poc: Poc) -> None:
    assert poc.non_repo_outcome == "not a git repository"
