import json
from pathlib import Path

import pytest

from ccp.capture.snapshots.git import Git, GitError
from ccp.capture.snapshots.push import PushQueue, push_refs, repository_name, resolve_remote

from .conftest import World, git, init_repo, make_service

SID = "sess-p"


# ---------------------------------------------------------------- which capture project


def test_the_repository_name_comes_from_the_origin_url(world: World) -> None:
    git(
        world.repo,
        "remote",
        "set-url",
        "origin",
        "https://gitlab.example.com/team/payments-api.git",
    )
    assert repository_name(Git(world.repo)) == "payments-api"


def test_the_repository_name_falls_back_to_the_folder_name(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "my repo!")  # no origin; unusual characters
    assert repository_name(Git(repo)) == "my-repo"


@pytest.mark.parametrize(
    "origin", ["git@host:group/proj.git", "C:\\work\\proj.git", "/srv/git/proj.git/"]
)
def test_names_are_found_in_every_kind_of_remote_address(world: World, origin: str) -> None:
    git(world.repo, "remote", "set-url", "origin", origin)
    assert repository_name(Git(world.repo)) == "proj"


def test_an_explicit_remote_wins_over_the_base(world: World) -> None:
    assert resolve_remote(Git(world.repo), "https://x/y.git", "https://base") == "https://x/y.git"


def test_the_base_address_gets_the_repository_name_appended(world: World) -> None:
    git(
        world.repo,
        "remote",
        "set-url",
        "origin",
        "https://gitlab.example.com/team/payments-api.git",
    )
    assert resolve_remote(Git(world.repo), None, "https://gitlab.example.com/claude-capture/") == (
        "https://gitlab.example.com/claude-capture/payments-api-capture.git"
    )


def test_no_settings_means_no_remote(world: World) -> None:
    assert resolve_remote(Git(world.repo), None, None) is None


# ---------------------------------------------------------------- the retry queue


def test_a_queued_push_is_retried_and_removed_when_it_succeeds(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "q.jsonl")
    queue.add(tmp_path, ["a:a"])
    assert len(queue) == 1
    assert queue.retry_all(lambda root, refs: True) == 1
    assert len(queue) == 0


def test_a_queued_push_that_still_fails_stays_queued(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "q.jsonl")
    queue.add(tmp_path, ["a:a"])
    queue.add(tmp_path, ["b:b"])
    assert queue.retry_all(lambda root, refs: refs == ["a:a"]) == 1  # only the first succeeds
    assert len(queue) == 1


def test_the_queue_never_stores_the_remote_address(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "q.jsonl")
    queue.add(tmp_path, ["refs/x:refs/x"])
    entry = json.loads((tmp_path / "q.jsonl").read_text(encoding="utf-8"))
    assert set(entry) == {"root", "refspecs"}  # a remote URL can carry credentials


def test_damaged_lines_are_dropped_and_do_not_block_the_queue(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    good_entry = json.dumps({"root": tmp_path.as_posix(), "refspecs": ["a:a"]})
    path.write_text(f"not json\n{good_entry}\n", encoding="utf-8")
    assert PushQueue(path).retry_all(lambda root, refs: True) == 1
    assert path.read_text(encoding="utf-8") == ""


def test_a_repository_that_no_longer_exists_is_dropped(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "q.jsonl")
    queue.add(tmp_path / "deleted-repo", ["a:a"])
    assert queue.retry_all(lambda root, refs: True) == 0
    assert len(queue) == 0


def test_an_empty_or_missing_queue_is_fine(tmp_path: Path) -> None:
    queue = PushQueue(tmp_path / "none.jsonl")
    assert (len(queue), queue.retry_all(lambda root, refs: True)) == (0, 0)


# ---------------------------------------------------------------- pushing


def test_many_refs_are_pushed_in_batches(world: World) -> None:
    for n in range(95):
        git(world.repo, "update-ref", f"refs/claude-capture/test/{n:03d}", world.start)
    refspecs = [
        f"refs/claude-capture/test/{n:03d}:refs/claude-capture/test/{n:03d}" for n in range(95)
    ]
    assert push_refs(Git(world.repo), world.capture.as_posix(), refspecs, timeout=60) is True
    assert len(world.snapshot_refs()) == 95


def test_a_failed_push_returns_false_and_never_raises(world: World) -> None:
    assert (
        push_refs(Git(world.repo), (world.root / "nope.git").as_posix(), ["HEAD:refs/x"], 10)
        is False
    )


def test_a_push_failure_never_leaks_a_remote_url_with_credentials(world: World) -> None:
    secret_url = "https://user:" + "topsecret-password" + "@127.0.0.1:1/x.git"
    service = make_service(world.state_dir, remote=secret_url)
    (world.repo / "app.py").write_text("edited\n", encoding="utf-8")
    outcome = service.snapshot(SID, world.repo, "Stop")

    assert outcome is not None and "queued" in outcome
    queued = (world.state_dir / "pending-pushes.jsonl").read_text(encoding="utf-8")
    assert "topsecret-password" not in outcome and "topsecret-password" not in queued


def test_git_errors_never_contain_git_output(world: World) -> None:
    with pytest.raises(GitError) as error:
        Git(world.repo).run(
            "push",
            "-q",
            "https://user:" + "leaky-token" + "@127.0.0.1:1/x.git",
            "HEAD:refs/x",
            timeout=10,
        )
    assert "leaky-token" not in str(error.value) and "127.0.0.1" not in str(error.value)


def test_a_git_command_that_takes_too_long_is_reported_not_waited_for(world: World) -> None:
    # A 1-millisecond limit cannot be met by any real command.
    with pytest.raises(GitError, match="timed out"):
        Git(world.repo, timeout=0.001).run("status")


def test_the_queue_is_retried_with_the_current_settings_at_the_next_start(world: World) -> None:
    broken = make_service(world.state_dir, remote=world.root / "nope.git")
    (world.repo / "app.py").write_text("edited\n", encoding="utf-8")
    broken.snapshot(SID, world.repo, "Stop")
    assert world.snapshot_refs() == []  # nothing arrived

    working = make_service(world.state_dir, remote=world.capture)  # the project now exists
    outcome = working.start("sess-q", world.repo)
    assert outcome is not None and "1 queued push(es) delivered" in outcome
    assert f"refs/claude-capture/sessions/{SID}/0001" in world.snapshot_refs()
