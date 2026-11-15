"""The job loop and the session indexer, with a fake store and a fake repository."""

import json
from collections.abc import Sequence

import pytest

from ccp.index.agents import AgentMeta
from ccp.index.parser import ParsedEntry
from ccp.index.repository import IndexJob, IndexRepositoryError
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
        self.agents: dict[tuple[str, str], AgentMeta] = {}

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

    def save_agent_meta(self, session_id: str, agent_id: str, meta: AgentMeta) -> None:
        self.agents[(session_id, agent_id)] = meta

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


def test_a_line_that_is_not_utf8_is_skipped_and_the_job_succeeds() -> None:
    chunk = _line(type="user", uuid="u1") + b"\xff\xfe\n" + _line(type="user", uuid="u3")
    repository = FakeRepository([IndexJob(7, "s1", 1)])

    process_next_job(FakeStore({MAIN_0: chunk}), repository)

    assert (repository.finished, repository.failed) == ([7], [])
    saved = repository.saved[MAIN_0]
    assert [(number, entry.entry_uuid) for number, entry in saved] == [(1, "u1"), (3, "u3")]


def test_a_line_that_is_not_utf8_is_counted_as_skipped() -> None:
    chunk = b"\xff\n" + _line(type="user") + b"broken\n"

    result = index_session(FakeStore({MAIN_0: chunk}), FakeRepository(), "s1")

    assert (result.events_stored, result.lines_skipped) == (1, 2)


def test_multi_byte_characters_keep_their_lines_and_line_numbers() -> None:
    """Splitting the bytes, not the decoded text, gives the same lines for valid UTF-8."""
    texts = ["héllo wörld", "日本語 テキスト", "emoji \U0001f600"]
    chunk = b"".join(
        (
            json.dumps({"type": "user", "message": {"content": text}}, ensure_ascii=False) + "\n"
        ).encode()
        for text in texts
    )
    assert not chunk.isascii()
    repository = FakeRepository()

    index_session(FakeStore({MAIN_0: chunk}), repository, "s1")

    saved = repository.saved[MAIN_0]
    assert [number for number, _ in saved] == [1, 2, 3]
    assert [entry.payload["message"]["content"] for _, entry in saved] == texts


class BrokenDatabase(FakeRepository):
    """A repository whose database is down for the first ``outages`` claims."""

    def __init__(self, jobs: list[IndexJob] | None = None, outages: int = 1) -> None:
        super().__init__(jobs)
        self.outages = outages

    def claim_job(self) -> IndexJob | None:
        if self.outages:
            self.outages -= 1
            raise IndexRepositoryError("database connection failed: InterfaceError")
        return super().claim_job()


def test_run_survives_a_database_failure_and_goes_on_after_waiting() -> None:
    class StopLoopError(Exception):
        pass

    repository = BrokenDatabase([IndexJob(1, "s1", 1)], outages=2)
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 3:  # two waits after the failures, then one on the empty queue
            raise StopLoopError

    with pytest.raises(StopLoopError):
        run(FakeStore({}), repository, poll_seconds=5, sleep=sleep)

    assert (repository.finished, waits) == ([1], [5, 5, 5])


def test_run_survives_a_failure_while_recording_the_outcome() -> None:
    class StopLoopError(Exception):
        pass

    class BrokenFinish(FakeRepository):
        def finish_job(self, job_id: int) -> None:
            raise IndexRepositoryError("finishing a job failed: InterfaceError")

    def sleep(seconds: float) -> None:
        raise StopLoopError

    repository = BrokenFinish([IndexJob(1, "s1", 1), IndexJob(2, "s1", 1)])

    with pytest.raises(StopLoopError):
        run(FakeStore({}), repository, poll_seconds=5, sleep=sleep)

    assert [job.job_id for job in repository.jobs] == [2]  # the loop was still running


def test_run_once_stops_with_the_error_when_the_database_fails() -> None:
    waits: list[float] = []

    with pytest.raises(IndexRepositoryError):
        run(FakeStore({}), BrokenDatabase(), poll_seconds=5, once=True, sleep=waits.append)

    assert waits == []


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


META = "sessions/s1/agent-a1.meta/000000000000.jsonl"


def test_a_sub_agent_description_is_stored_as_a_run_and_not_as_an_event() -> None:
    line = json.dumps({"agentType": "Explore", "toolUseId": "toolu_1", "spawnDepth": 1}) + "\n"
    store = FakeStore({MAIN_0: _line(type="user"), AGENT: _line(type="user"), META: line.encode()})
    repository = FakeRepository()

    result = index_session(store, repository, "s1")

    assert set(repository.saved) == {MAIN_0, AGENT}
    assert (result.chunks_indexed, result.lines_skipped) == (2, 0)
    assert repository.agents[("s1", "a1")].tool_use_id == "toolu_1"


def test_a_description_is_read_again_each_time_because_it_is_not_tracked() -> None:
    store = FakeStore({META: b'{"agentType": "Explore"}\n'})
    repository = FakeRepository()
    index_session(store, repository, "s1")
    store.reads.clear()

    index_session(store, repository, "s1")

    assert store.reads == [META]


@pytest.mark.parametrize("content", [b"not json\n", b"[1]\n", b"\xff\n"])
def test_an_unreadable_description_is_skipped_and_counted(content: bytes) -> None:
    repository = FakeRepository()

    result = index_session(FakeStore({META: content}), repository, "s1")

    assert (repository.agents, result.lines_skipped) == ({}, 1)
