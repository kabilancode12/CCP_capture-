import os
import time
from pathlib import Path

import pytest

from ccp.capture.state import STALE_LOCK_SECONDS, StateStore


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state")


def test_an_unknown_stream_starts_at_zero(state: StateStore) -> None:
    assert state.get_offset("s1", "main") == 0


def test_an_offset_can_be_saved_and_read_back(state: StateStore) -> None:
    state.set_offset("s1", "main", 1234)
    state.set_offset("s1", "agent-a", 7)
    assert (state.get_offset("s1", "main"), state.get_offset("s1", "agent-a")) == (1234, 7)
    assert state.get_offset("s2", "main") == 0  # other sessions are independent


def test_saving_leaves_no_temporary_file_behind(state: StateStore, tmp_path: Path) -> None:
    state.set_offset("s1", "main", 5)
    state.set_offset("s1", "main", 9)
    assert sorted(p.name for p in (tmp_path / "state" / "s1").iterdir()) == ["main.offset"]


def test_a_corrupt_offset_file_means_start_again_from_zero(
    state: StateStore, tmp_path: Path
) -> None:
    state.set_offset("s1", "main", 5)
    (tmp_path / "state" / "s1" / "main.offset").write_text("garbage", encoding="utf-8")
    assert state.get_offset("s1", "main") == 0  # uploads are idempotent, so this is safe


def test_unsafe_names_cannot_escape_the_state_folder(state: StateStore) -> None:
    with pytest.raises(ValueError):
        state.set_offset("../evil", "main", 1)
    with pytest.raises(ValueError):
        state.get_offset("s1", "../../evil")


def test_remembered_transcripts_are_listed(state: StateStore, tmp_path: Path) -> None:
    assert state.known_sessions() == []
    state.remember_transcript("s1", tmp_path / "a.jsonl")
    state.remember_transcript("s2", tmp_path / "b.jsonl")
    assert state.known_sessions() == [("s1", tmp_path / "a.jsonl"), ("s2", tmp_path / "b.jsonl")]


def test_only_the_position_is_stored_never_transcript_content(
    state: StateStore, tmp_path: Path
) -> None:
    state.set_offset("s1", "main", 5)
    state.remember_transcript("s1", tmp_path / "a.jsonl")
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert len(path.read_text(encoding="utf-8")) < 200  # a number or a path, nothing more


# ---------------------------------------------------------------- locking


def test_a_lock_is_exclusive_and_released(state: StateStore, tmp_path: Path) -> None:
    with state.lock("s1", "main") as first:
        assert first is True
        with state.lock("s1", "main", wait_seconds=0.1) as second:
            assert second is False  # someone else holds it
        with state.lock("s1", "agent-a", wait_seconds=0.1) as other_stream:
            assert other_stream is True  # different streams do not block each other
    assert not (tmp_path / "state" / "s1" / "main.lock").exists()
    with state.lock("s1", "main") as again:
        assert again is True


def test_the_lock_is_released_even_if_the_work_fails(state: StateStore) -> None:
    with pytest.raises(RuntimeError), state.lock("s1", "main"):
        raise RuntimeError("boom")
    with state.lock("s1", "main", wait_seconds=0.1) as acquired:
        assert acquired is True


def test_a_stale_lock_from_a_crashed_process_is_taken_over(
    state: StateStore, tmp_path: Path
) -> None:
    lock_file = tmp_path / "state" / "s1" / "main.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("12345", encoding="utf-8")
    old = time.time() - STALE_LOCK_SECONDS - 10
    os.utime(lock_file, (old, old))

    with state.lock("s1", "main", wait_seconds=0.1) as acquired:
        assert acquired is True
