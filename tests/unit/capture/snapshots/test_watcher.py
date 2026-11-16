"""The watcher and the global Git hooks (real Git)."""

from pathlib import Path

import pytest

from ccp.capture.githooks import install, uninstall
from ccp.capture.hooks import TOOL_BUSY_MARKER
from ccp.capture.state import StateStore
from ccp.capture.watcher import Watcher

from .conftest import World, git, make_service

SID = "sess-watch"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _watcher(world: World, clock: Clock) -> tuple[Watcher, StateStore]:
    state = StateStore(world.state_dir)
    service = make_service(world.state_dir, remote=world.capture)
    return Watcher(service, state, world.state_dir, quiet_seconds=60, clock=clock), state


def test_a_quiet_change_made_outside_claude_is_recorded_as_a_persons(world: World) -> None:
    clock = Clock()
    watcher, _ = _watcher(world, clock)
    service = make_service(world.state_dir, remote=world.capture)
    service.snapshot(SID, world.repo, "SessionStart")  # a hook has seen the repository
    (world.repo / "notes.txt").write_text("written by hand\n", encoding="utf-8")

    watcher.tick()  # first look: remembers the state
    clock.now += 30
    assert not any("snapshot" in n for n in watcher.tick())  # not quiet for 60 s yet
    clock.now += 60
    notes = watcher.tick()
    assert any("snapshot" in n for n in notes)
    (branch,) = git(
        world.capture, "for-each-ref", "--format=%(refname)", "refs/heads/ccp"
    ).splitlines()
    assert "[Human" in git(world.capture, "log", "-1", "--format=%s", branch)
    assert "notes.txt" in git(world.capture, "show", "--stat", "--format=", branch)
    clock.now += 600
    assert not any("snapshot" in n for n in watcher.tick())  # nothing new: nothing recorded again


def test_the_watcher_waits_while_a_claude_tool_call_is_running(world: World) -> None:
    clock = Clock()
    watcher, state = _watcher(world, clock)
    make_service(world.state_dir, remote=world.capture).snapshot(SID, world.repo, "SessionStart")
    (world.repo / "by_claude.txt").write_text("x\n", encoding="utf-8")
    state.set_marker(SID, TOOL_BUSY_MARKER, str(clock.now))
    clock.now += 120
    assert watcher.tick() == ["a Claude tool call is running: waiting"]
    assert git(world.capture, "for-each-ref", "refs/heads/ccp") == ""


def test_git_hooks_are_written_and_never_replace_a_hooks_path_that_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    folder = home / "hooks"
    notes = install(folder)
    assert (folder / "post-commit").is_file() and (folder / "pre-push").is_file()
    assert "core.hooksPath set (global)" in notes
    assert "already points here" in "\n".join(install(folder))
    uninstall(folder)
    assert not (folder / "post-commit").exists()
    other = home / "theirs"
    install(other)  # sets the path to `other`
    assert "left unchanged" in "\n".join(install(folder))
