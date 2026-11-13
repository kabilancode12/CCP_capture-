"""The job loop and the session indexer, with a fake store and a fake repository."""

import json
from collections.abc import Sequence

import pytest

from ccp.index.parser import ParsedEntry
from ccp.index.repository import IndexJob
from ccp.index.worker import index_session, process_next_job, run


def _line(**entry: object) -> bytes:
    return (json.dumps(entry) + "\n").encode()


class FakeStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self.objects[key]


class FakeRepository:
    def __init__(self, jobs: list[IndexJob] | None = None) -> None:
        self.jobs = jobs or []
        self.saved: dict[str, list[tuple[int, ParsedEntry]]] = {}
        self.finished: list[int] = []
        self.failed: list[int] = []
        self.retry = True
        self.refreshed: list[str] = []

    def claim_job(self) -> IndexJob | None:
        return self.jobs.pop(0) if self.jobs else None

    def finish_job(self, job_id: int) -> None:
        self.finished.append(job_id)

    def fail_job(self, job_id: int) -> bool:
        self.failed.append(job_id)
        return self.retry

    def indexed_keys(self, session_id: str) -> set[str]:
        return set(self.saved)

    def refresh_session(self, session_id: str) -> None:
        self.refreshed.append(session_id)

    def save_chunk(
        self, session_id: str, object_key: str, entries: Sequence[tuple[int, ParsedEntry]]
    ) -> None:
        self.saved[object_key] = list(entries)


MAIN_0 = "sessions/s1/main/000000000000.jsonl"
MAIN_1 = "sessions/s1/main/000000000100.jsonl"
AGENT = "sessions/s1/agent-a1/000000000000.jsonl"


def test_every_chunk_of_the_session_is_indexed_with_line_numbers_from_one() -> None:
    store = FakeStore(
        {
            MAIN_0: _line(type="user", uuid="u1") + _line(type="assistant", uuid="a1"),
            AGENT: _line(type="user", isSidechain=True),
            "sessions/other/main/000000000000.jsonl": _line(type="user"),
        }
    )
    repository = FakeRepository()

    result = index_session(store, repository, "s1")

    assert (result.chunks_indexed, result.events_stored) == (2, 3)
    assert [number for number, _ in repository.saved[MAIN_0]] == [1, 2]
    assert set(repository.saved) == {MAIN_0, AGENT}


def test_chunks_that_are_already_indexed_are_not_read_again() -> None:
    store = FakeStore({MAIN_0: _line(type="user")})
    repository = FakeRepository()
    index_session(store, repository, "s1")
    store.objects[MAIN_1] = _line(type="assistant")
    store.reads.clear()

    result = index_session(store, repository, "s1")

    assert (result.chunks_indexed, store.reads) == (1, [MAIN_1])
    assert index_session(store, repository, "s1").chunks_indexed == 0


def test_other_objects_under_the_session_are_ignored() -> None:
    store = FakeStore({"sessions/s1/notes.txt": b"x", MAIN_0: _line(type="user")})
    assert index_session(store, FakeRepository(), "s1").chunks_indexed == 1


def test_a_line_that_cannot_be_read_is_counted_and_keeps_the_line_numbers() -> None:
    store = FakeStore({MAIN_0: _line(type="user") + b"broken\n" + _line(type="user")})
    repository = FakeRepository()

    result = index_session(store, repository, "s1")

    assert (result.events_stored, result.lines_skipped) == (2, 1)
    assert [number for number, _ in repository.saved[MAIN_0]] == [1, 3]


def test_a_session_with_no_chunks_indexes_nothing() -> None:
    assert index_session(FakeStore({}), FakeRepository(), "s1").chunks_indexed == 0


def test_a_finished_job_is_marked_done() -> None:
    repository = FakeRepository([IndexJob(7, "s1", 1)])
    assert process_next_job(FakeStore({MAIN_0: _line(type="user")}), repository) is True
    assert repository.finished == [7]
    assert repository.failed == []


def test_an_empty_queue_is_reported() -> None:
    assert process_next_job(FakeStore({}), FakeRepository()) is False


def test_a_failing_session_fails_its_job_and_does_not_stop_the_caller() -> None:
    class BrokenStore(FakeStore):
        def get(self, key: str) -> bytes:
            raise OSError("storage is down")

    repository = FakeRepository([IndexJob(7, "s1", 1)])

    assert process_next_job(BrokenStore({MAIN_0: b""}), repository) is True
    assert (repository.failed, repository.finished) == ([7], [])


def test_a_chunk_that_is_not_utf8_fails_the_job() -> None:
    repository = FakeRepository([IndexJob(7, "s1", 1)])
    process_next_job(FakeStore({MAIN_0: b"\xff\n"}), repository)
    assert repository.failed == [7]


def test_run_once_handles_every_queued_job_then_stops() -> None:
    repository = FakeRepository([IndexJob(1, "s1", 1), IndexJob(2, "s1", 1)])
    waits: list[float] = []
    handled = run(FakeStore({}), repository, poll_seconds=5, once=True, sleep=waits.append)
    assert (handled, repository.finished, waits) == (2, [1, 2], [])


def test_run_waits_when_the_queue_is_empty() -> None:
    class StopLoopError(Exception):
        pass

    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            raise StopLoopError

    with pytest.raises(StopLoopError):
        run(FakeStore({}), FakeRepository(), poll_seconds=5, sleep=sleep)
    assert waits == [5, 5]


def test_the_derived_data_is_refreshed_after_every_run_even_when_nothing_was_new() -> None:
    store = FakeStore({MAIN_0: _line(type="user")})
    repository = FakeRepository()

    index_session(store, repository, "s1")
    index_session(store, repository, "s1")  # nothing new the second time

    assert repository.refreshed == ["s1", "s1"]


def test_a_refresh_that_fails_fails_the_job() -> None:
    class BrokenRefresh(FakeRepository):
        def refresh_session(self, session_id: str) -> None:
            raise RuntimeError("database is down")

    repository = BrokenRefresh([IndexJob(7, "s1", 1)])

    process_next_job(FakeStore({MAIN_0: _line(type="user")}), repository)

    assert (repository.failed, repository.finished) == ([7], [])
