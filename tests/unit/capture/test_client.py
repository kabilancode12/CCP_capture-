"""The sync logic, tested with a fake API and real files in a temporary folder."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from ccp.capture.api import ApiRejectedError, ApiUnavailableError
from ccp.capture.client import CaptureClient
from ccp.capture.state import StateStore
from ccp.redaction.records import RedactionRecord
from ccp.storage.attachments import MIN_EXTERNAL_BYTES

SESSION = "sess-1"


class FakeApi:
    """Records every call, in order; can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (kind, detail)
        self.chunks: dict[tuple[str, int], bytes] = {}  # (stream, offset) -> body
        self.attachments: dict[str, bytes] = {}
        self.redactions: list[RedactionRecord] = []
        self.fail_with: Exception | None = None
        self.fail_on: str | None = None  # a kind: "chunk", "attachment" or "redactions"

    def _maybe_fail(self, kind: str) -> None:
        if self.fail_with is not None and self.fail_on in (None, kind):
            raise self.fail_with

    def put_chunk(self, session_id: str, stream: str, offset: int, body: bytes) -> bool:
        self._maybe_fail("chunk")
        self.calls.append(("chunk", f"{stream}@{offset}"))
        new = (stream, offset) not in self.chunks
        self.chunks[(stream, offset)] = body
        return new

    def put_attachment(self, digest: str, body: bytes) -> bool:
        self._maybe_fail("attachment")
        self.calls.append(("attachment", digest[:8]))
        new = digest not in self.attachments
        self.attachments[digest] = body
        return new

    def post_redactions(self, session_id: str, records: Sequence[RedactionRecord]) -> None:
        self._maybe_fail("redactions")
        self.calls.append(("redactions", str(len(records))))
        self.redactions.extend(records)


class Clock:
    """A clock that only moves when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state")


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    return tmp_path / "projects" / f"{SESSION}.jsonl"


def _write(path: Path, *entries: object, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries)
    with path.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _client(api: FakeApi, state: StateStore, **options: object) -> CaptureClient:
    return CaptureClient(api, state, **options)  # type: ignore[arg-type]


def _entry(n: int) -> dict[str, object]:
    return {"type": "user", "uuid": f"u{n}", "message": f"hello {n}"}


# ---------------------------------------------------------------- the normal path


def test_new_lines_are_uploaded_and_the_position_is_saved(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1), _entry(2))
    (result,) = _client(api, state).sync_session(SESSION, transcript)

    assert (result.chunks, result.caught_up, result.error) == (1, True, None)
    assert list(api.chunks) == [("main", 0)]
    assert api.chunks[("main", 0)].decode().count("\n") == 2
    assert state.get_offset(SESSION, "main") == transcript.stat().st_size


def test_a_second_sync_with_nothing_new_uploads_nothing(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    client = _client(api, state)
    client.sync_session(SESSION, transcript)
    api.calls.clear()

    (result,) = client.sync_session(SESSION, transcript)
    assert api.calls == [] and result.caught_up


def test_appended_lines_go_in_a_new_chunk_at_the_previous_end(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    client = _client(api, state)
    client.sync_session(SESSION, transcript)
    first_end = transcript.stat().st_size

    _write(transcript, _entry(2), append=True)
    client.sync_session(SESSION, transcript)

    assert sorted(api.chunks) == [("main", 0), ("main", first_end)]
    assert b"hello 2" in api.chunks[("main", first_end)]
    assert b"hello 1" not in api.chunks[("main", first_end)]  # nothing is sent twice


def test_a_half_written_last_line_waits_until_it_is_complete(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"user","uu')  # Claude Code is mid-write
    client = _client(api, state)
    client.sync_session(SESSION, transcript)
    assert b"hello 1" in api.chunks[("main", 0)] and len(api.chunks) == 1

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write('id":"u2"}\n')
    client.sync_session(SESSION, transcript)
    assert len(api.chunks) == 2


def test_small_chunks_are_numbered_by_their_source_offsets(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, *[_entry(n) for n in range(1, 5)])
    line_size = len(json.dumps(_entry(1), separators=(",", ":"))) + 1
    _client(api, state, chunk_bytes=line_size * 2).sync_session(SESSION, transcript)

    assert sorted(api.chunks) == [("main", 0), ("main", line_size * 2)]


def test_blank_lines_alone_advance_the_position_without_an_upload(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"\n\n")
    _client(api, state).sync_session(SESSION, transcript)
    assert api.calls == []
    assert state.get_offset(SESSION, "main") == 2


# ---------------------------------------------------------------- order of steps


def test_attachments_go_first_then_the_chunk_then_the_report_then_the_position_moves(
    api: FakeApi, state: StateStore, transcript: Path, fakes: dict[str, str]
) -> None:
    big = {"type": "skill_listing", "text": "x" * (MIN_EXTERNAL_BYTES * 2)}
    _write(
        transcript,
        {"type": "attachment", "uuid": "u1", "sessionId": SESSION, "attachment": big},
        {"type": "user", "uuid": "u2", "message": fakes["email"]},
    )
    _client(api, state).sync_session(SESSION, transcript)

    assert [kind for kind, _ in api.calls] == ["attachment", "chunk", "redactions"]
    assert api.redactions[0].rule_id == "email"
    assert fakes["email"].encode() not in api.chunks[("main", 0)]


def test_if_the_report_fails_the_position_does_not_move_and_a_retry_completes_it(
    api: FakeApi, state: StateStore, transcript: Path, fakes: dict[str, str]
) -> None:
    _write(transcript, {"type": "user", "uuid": "u1", "message": fakes["email"]})
    client = _client(api, state)

    api.fail_with, api.fail_on = ApiUnavailableError("db down"), "redactions"
    (failed,) = client.sync_session(SESSION, transcript)
    assert failed.error is not None and failed.chunks == 0
    assert state.get_offset(SESSION, "main") == 0  # so nothing is forgotten

    api.fail_with = None
    (retry,) = client.sync_session(SESSION, transcript)
    assert retry.error is None and len(api.redactions) == 1
    assert state.get_offset(SESSION, "main") == transcript.stat().st_size


# ---------------------------------------------------------------- when the API is not there


def test_an_unavailable_api_loses_nothing(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1), _entry(2))
    client = _client(api, state)

    api.fail_with = ApiUnavailableError("connection refused")
    (down,) = client.sync_session(SESSION, transcript)
    assert "unavailable" in (down.error or "") and not down.caught_up
    assert state.get_offset(SESSION, "main") == 0 and api.chunks == {}

    api.fail_with = None  # the API is back
    (up,) = client.sync_session(SESSION, transcript)
    assert up.error is None and list(api.chunks) == [("main", 0)]  # the same chunk, same place
    assert api.chunks[("main", 0)].decode().count("\n") == 2


def test_a_refused_upload_stops_and_reports_without_moving_the_position(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    api.fail_with = ApiRejectedError(401, "missing or invalid token")
    (result,) = _client(api, state).sync_session(SESSION, transcript)
    assert "401" in (result.error or "")
    assert state.get_offset(SESSION, "main") == 0


def test_a_chunk_that_failed_is_retried_not_skipped_when_later_ones_exist(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, *[_entry(n) for n in range(1, 5)])
    line_size = len(json.dumps(_entry(1), separators=(",", ":"))) + 1
    client = _client(api, state, chunk_bytes=line_size * 2)

    original_put = api.put_chunk
    count = {"n": 0}

    def flaky(session_id: str, stream: str, offset: int, body: bytes) -> bool:
        count["n"] += 1
        if count["n"] == 2:  # the second chunk fails once
            raise ApiUnavailableError("blip")
        return original_put(session_id, stream, offset, body)

    api.put_chunk = flaky  # type: ignore[method-assign]
    client.sync_session(SESSION, transcript)
    assert list(api.chunks) == [("main", 0)]  # the first chunk went through
    assert state.get_offset(SESSION, "main") == line_size * 2  # and only it is remembered

    client.sync_session(SESSION, transcript)
    assert sorted(api.chunks) == [("main", 0), ("main", line_size * 2)]


def test_starting_over_with_no_saved_state_resends_the_same_chunks(
    api: FakeApi, state: StateStore, transcript: Path, tmp_path: Path
) -> None:
    _write(transcript, _entry(1), _entry(2))
    _client(api, state).sync_session(SESSION, transcript)
    before = dict(api.chunks)

    fresh_state = StateStore(tmp_path / "other-state")  # as if the state folder was lost
    _client(api, fresh_state).sync_session(SESSION, transcript)
    assert (
        api.chunks == before
    )  # identical chunks at identical offsets: the server sees "already stored"


# ---------------------------------------------------------------- time, locks, bad files


def test_a_sync_that_runs_out_of_time_stops_cleanly_and_the_next_one_continues(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, *[_entry(n) for n in range(1, 5)])
    line_size = len(json.dumps(_entry(1), separators=(",", ":"))) + 1
    clock = Clock()
    client = _client(api, state, chunk_bytes=line_size, budget_seconds=10.0, clock=clock)

    original_put = api.put_chunk

    def slow(session_id: str, stream: str, offset: int, body: bytes) -> bool:
        clock.now += 6.0  # every upload takes 6 seconds
        return original_put(session_id, stream, offset, body)

    api.put_chunk = slow  # type: ignore[method-assign]
    (first,) = client.sync_session(SESSION, transcript)
    assert first.chunks == 2 and not first.caught_up and first.error is None

    clock.now = 100.0  # later: a fresh time budget
    (second,) = client.sync_session(SESSION, transcript)
    assert second.chunks == 2 and second.caught_up
    assert len(api.chunks) == 4


def test_a_stream_that_is_being_synced_elsewhere_is_skipped(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    with state.lock(SESSION, "main"):
        results = _client(api, state, lock_wait_seconds=0.1).sync_session(SESSION, transcript)
    assert results[0].error == "another sync of this stream is running"
    assert api.calls == []


def test_a_missing_transcript_is_reported_not_raised(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    (result,) = _client(api, state).sync_session(SESSION, transcript)
    assert "FileNotFoundError" in (result.error or "")


def test_a_transcript_that_shrank_is_reported_and_nothing_is_uploaded(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    state.set_offset(SESSION, "main", 10_000)  # more than the file holds
    (result,) = _client(api, state).sync_session(SESSION, transcript)
    assert "TranscriptError" in (result.error or "")
    assert api.calls == []


# ---------------------------------------------------------------- sub-agents


def _agent_files(transcript: Path, agent: str = "agent-a7c5c9") -> tuple[Path, Path]:
    folder = transcript.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    return folder / f"{agent}.jsonl", folder / f"{agent}.meta.json"


def test_sub_agent_transcripts_and_meta_files_are_uploaded_as_their_own_streams(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    agent_log, agent_meta = _agent_files(transcript)
    _write(agent_log, {"type": "user", "uuid": "a1", "isSidechain": True})
    agent_meta.write_text('{"agentType":"Explore","toolUseId":"toolu_1"}', encoding="utf-8")

    results = _client(api, state).sync_session(SESSION, transcript)

    assert [r.stream for r in results] == ["main", "agent-a7c5c9", "agent-a7c5c9.meta"]
    assert sorted(api.chunks) == [("agent-a7c5c9", 0), ("agent-a7c5c9.meta", 0), ("main", 0)]
    assert json.loads(api.chunks[("agent-a7c5c9.meta", 0)])["toolUseId"] == "toolu_1"


def test_a_meta_file_is_uploaded_once(api: FakeApi, state: StateStore, transcript: Path) -> None:
    _write(transcript, _entry(1))
    _, agent_meta = _agent_files(transcript)
    agent_meta.write_text('{"agentType":"Explore"}', encoding="utf-8")
    client = _client(api, state)
    client.sync_session(SESSION, transcript)
    api.calls.clear()

    client.sync_session(SESSION, transcript)
    assert api.calls == []


def test_a_sub_agent_with_an_unsafe_file_name_is_skipped(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    folder = transcript.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    (folder / "agent-bad name.jsonl").write_text('{"a":1}\n', encoding="utf-8")

    results = _client(api, state).sync_session(SESSION, transcript)
    bad = next(r for r in results if r.stream == "agent-bad name")
    assert bad.error == "stream name is not allowed"
    assert ("agent-bad name", 0) not in api.chunks


# ---------------------------------------------------------------- catch-up


def test_catch_up_uploads_what_earlier_sessions_left_behind(
    api: FakeApi, state: StateStore, tmp_path: Path
) -> None:
    first, second = tmp_path / "a" / "s-a.jsonl", tmp_path / "b" / "s-b.jsonl"
    _write(first, _entry(1))
    _write(second, _entry(2))
    api.fail_with = ApiUnavailableError("down")
    _client(api, state).sync_session("s-a", first)
    _client(api, state).sync_session("s-b", second)
    api.fail_with = None
    _write(second, _entry(3), append=True)  # and more arrives later

    results = _client(api, state).catch_up()

    assert sorted(results) == ["s-a", "s-b"]
    chunk_calls = [detail for kind, detail in api.calls if kind == "chunk"]
    assert chunk_calls == ["main@0", "main@0"]  # one chunk per session, both at offset 0
    assert b"hello 3" in api.chunks[("main", 0)]  # session b's later line was caught up too


def test_catch_up_skips_sessions_whose_transcript_is_gone(
    api: FakeApi, state: StateStore, tmp_path: Path
) -> None:
    gone = tmp_path / "gone.jsonl"
    _write(gone, _entry(1))
    _client(api, state).sync_session("s-gone", gone)
    gone.unlink()
    api.calls.clear()
    assert _client(api, state).catch_up() == {}
    assert api.calls == []
