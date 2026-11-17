"""The sync logic, tested with a fake API and real files in a temporary folder."""

import functools
import json
from pathlib import Path

import pytest

from ccp.capture import client as client_module
from ccp.capture.api import ApiRejectedError, ApiUnavailableError
from ccp.capture.client import MAX_REFUSALS, CaptureClient
from ccp.capture.reader import read_complete_lines
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.storage.attachments import MIN_EXTERNAL_BYTES

from .conftest import Clock, FakeApi

SESSION = "sess-1"


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
    api.fail_with = ApiRejectedError(422, "malformed chunk")
    (result,) = _client(api, state).sync_session(SESSION, transcript)
    assert "422" in (result.error or "")
    assert state.get_offset(SESSION, "main") == 0


# ---------------------------------------------------------------- a refused token


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_stops_the_run_and_says_which_setting_to_check(
    api: FakeApi, state: StateStore, transcript: Path, status: int
) -> None:
    _write(transcript, _entry(1))
    folder = transcript.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    _write(folder / "agent-a.jsonl", {"type": "user", "uuid": "a"})
    api.fail_with = ApiRejectedError(status, "missing or invalid token")

    results = _client(api, state).sync_session(SESSION, transcript)

    assert [r.stream for r in results] == ["main"]  # the sub-agent would be refused too
    (result,) = results
    assert result.access_denied and result.stops_run and not result.api_unavailable
    assert str(status) in (result.error or "") and "CCP_INGEST_TOKEN" in (result.error or "")
    assert state.get_offset(SESSION, "main") == 0  # nothing is skipped: the data waits


def test_catch_up_stops_at_the_first_refused_token(
    api: FakeApi, state: StateStore, tmp_path: Path
) -> None:
    for name in ("s-a", "s-b", "s-c"):
        path = tmp_path / name / f"{name}.jsonl"
        _write(path, _entry(1))
        state.remember_transcript(name, path)
    api.fail_with = ApiRejectedError(401, "missing or invalid token")

    results = _client(api, state).catch_up()

    assert list(results) == ["s-a"]  # the other sessions were not tried


def test_a_refused_token_is_never_counted_towards_skipping_a_chunk(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    api.fail_with = ApiRejectedError(401, "missing or invalid token")
    client = _client(api, state)
    for _ in range(MAX_REFUSALS + 2):
        client.sync_session(SESSION, transcript)
    assert api.chunks == {} and state.get_offset(SESSION, "main") == 0


# ---------------------------------------------------------------- a chunk the API keeps refusing


def _refuse_chunks_containing(api: FakeApi, marker: bytes, status: int = 413) -> None:
    """Make the fake API refuse every chunk whose body contains ``marker``."""
    original_put = api.put_chunk

    def picky(session_id: str, stream: str, offset: int, body: bytes) -> bool:
        if marker in body:
            raise ApiRejectedError(status, "no thanks")
        return original_put(session_id, stream, offset, body)

    api.put_chunk = picky  # type: ignore[method-assign]


def test_a_chunk_refused_three_times_is_replaced_by_a_placeholder_and_the_stream_moves_on(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1), _entry(2))
    line_size = len(json.dumps(_entry(1), separators=(",", ":"))) + 1
    client = _client(api, state, chunk_bytes=line_size)  # one line per chunk
    _refuse_chunks_containing(api, b"hello 1")

    for _ in range(MAX_REFUSALS - 1):  # the first refusals: the chunk is tried again later
        (result,) = client.sync_session(SESSION, transcript)
        assert "413" in (result.error or "") and result.skipped == 0
        assert state.get_offset(SESSION, "main") == 0 and api.chunks == {}

    (result,) = client.sync_session(SESSION, transcript)  # refused once more: skipped

    assert result.skipped == 1 and result.error is None and result.caught_up
    assert json.loads(api.chunks[("main", 0)]) == {
        "type": "ccp-withheld",
        "reason": "chunk refused by the API",
        "status": 413,
        "lines": 1,
        "bytes": line_size,
    }
    assert b"hello 2" in api.chunks[("main", line_size)]  # the later lines went through
    assert state.get_offset(SESSION, "main") == transcript.stat().st_size


def test_refusals_are_counted_per_chunk_and_start_again_for_the_next_one(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1), _entry(2))
    line_size = len(json.dumps(_entry(1), separators=(",", ":"))) + 1
    client = _client(api, state, chunk_bytes=line_size)

    api.fail_with = ApiRejectedError(422, "malformed")
    for _ in range(MAX_REFUSALS - 1):
        client.sync_session(SESSION, transcript)  # chunk 1 refused, not yet skipped
    api.fail_with = None
    _refuse_chunks_containing(api, b"hello 2", status=422)
    client.sync_session(SESSION, transcript)  # chunk 1 accepted; chunk 2 refused once

    assert b"hello 1" in api.chunks[("main", 0)]  # the real content, not a placeholder
    assert ("main", line_size) not in api.chunks  # chunk 2 is not skipped after one refusal
    assert state.get_offset(SESSION, "main") == line_size


def test_if_the_placeholder_is_refused_too_the_stream_still_moves_on(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    api.fail_with = ApiRejectedError(409, "a different chunk is already stored")
    client = _client(api, state)
    for _ in range(MAX_REFUSALS):
        (result,) = client.sync_session(SESSION, transcript)
    assert result.skipped == 1 and result.caught_up
    assert state.get_offset(SESSION, "main") == transcript.stat().st_size


def test_if_the_api_is_down_when_the_placeholder_is_sent_it_is_sent_next_time(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    client = _client(api, state)
    _refuse_chunks_containing(api, b"hello 1")
    for _ in range(MAX_REFUSALS - 1):
        client.sync_session(SESSION, transcript)

    refusing_put = api.put_chunk

    def down_for_placeholders(session_id: str, stream: str, offset: int, body: bytes) -> bool:
        if b"ccp-withheld" in body:
            raise ApiUnavailableError("down")
        return refusing_put(session_id, stream, offset, body)

    api.put_chunk = down_for_placeholders  # type: ignore[method-assign]
    (down,) = client.sync_session(SESSION, transcript)
    assert down.api_unavailable and state.get_offset(SESSION, "main") == 0

    api.put_chunk = refusing_put  # type: ignore[method-assign]
    (result,) = client.sync_session(SESSION, transcript)
    assert result.skipped == 1 and b"ccp-withheld" in api.chunks[("main", 0)]


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


def test_a_transcript_that_shrank_uploads_nothing_and_does_not_fail(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    state.set_offset(SESSION, "main", 10_000)  # more than the file holds
    (result,) = _client(api, state).sync_session(SESSION, transcript)
    assert result.error is None
    assert api.calls == []
    assert state.get_offset(SESSION, "main") == 10_000


def test_a_line_too_long_to_read_is_replaced_and_the_lines_after_it_are_uploaded(
    api: FakeApi, state: StateStore, transcript: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_window = functools.partial(read_complete_lines, window_bytes=200)
    monkeypatch.setattr(client_module, "read_complete_lines", small_window)
    huge = {"type": "user", "uuid": "u-huge", "message": "z" * 1000}
    _write(transcript, _entry(1), huge, _entry(2))

    (result,) = _client(api, state).sync_session(SESSION, transcript)

    uploaded = b"".join(api.chunks[key] for key in sorted(api.chunks))
    assert b"hello 1" in uploaded and b"hello 2" in uploaded  # one sync, all lines
    assert b"zzz" not in uploaded and b"line too long to read" in uploaded
    assert result.caught_up and state.get_offset(SESSION, "main") == transcript.stat().st_size


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


def test_sub_agents_started_by_the_workflow_tool_are_uploaded_too(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    run = transcript.with_suffix("") / "subagents" / "workflows" / "wf_1a2b-3c"
    run.mkdir(parents=True)
    _write(run / "agent-a4cd7b54d2b8ca0ae.jsonl", {"type": "user", "uuid": "w1"})
    (run / "agent-a4cd7b54d2b8ca0ae.meta.json").write_text('{"agentType":"x"}', encoding="utf-8")
    _write(run / "journal.jsonl", {"type": "step"})  # the workflow's own journal: not an agent

    results = _client(api, state).sync_session(SESSION, transcript)

    assert [r.stream for r in results] == [
        "main",
        "agent-a4cd7b54d2b8ca0ae",
        "agent-a4cd7b54d2b8ca0ae.meta",
    ]
    assert all(r.error is None and r.caught_up for r in results)
    assert ("agent-a4cd7b54d2b8ca0ae", 0) in api.chunks
    assert not any(b'"step"' in body for body in api.chunks.values())


def test_an_agent_id_found_twice_is_uploaded_from_one_file_only(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    direct, _ = _agent_files(transcript, "agent-dup")
    _write(direct, {"type": "user", "uuid": "first"})
    run = direct.parent / "workflows" / "wf_1"
    _write(run / "agent-dup.jsonl", {"type": "user", "uuid": "second"})

    results = _client(api, state).sync_session(SESSION, transcript)

    assert [r.stream for r in results] == ["main", "agent-dup"]
    assert b"first" in api.chunks[("agent-dup", 0)]


def test_a_meta_file_with_an_unsafe_name_is_skipped(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    _, meta = _agent_files(transcript, "agent-bad name")
    meta.write_text('{"agentType":"x"}', encoding="utf-8")

    results = _client(api, state).sync_session(SESSION, transcript)
    bad = next(r for r in results if r.stream == "agent-bad name.meta")
    assert bad.error == "stream name is not allowed"


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


# ---------------------------------------------------------------- the environment record


def test_the_environment_is_sent_once_and_not_again_while_it_is_unchanged(
    api: FakeApi, state: StateStore
) -> None:
    record = EnvironmentRecord(surface="terminal", os_name="Windows")
    client = _client(api, state)

    assert client.report_environment(SESSION, record) is None
    assert client.report_environment(SESSION, record) is None  # nothing new to say
    assert len(api.environments) == 1


def test_a_changed_environment_is_sent_again(api: FakeApi, state: StateStore) -> None:
    client = _client(api, state)
    client.report_environment(SESSION, EnvironmentRecord(surface="terminal"))
    client.report_environment(
        SESSION, EnvironmentRecord(surface="terminal", claude_code_version="2.1.289")
    )
    assert len(api.environments) == 2


def test_an_empty_environment_sends_nothing(api: FakeApi, state: StateStore) -> None:
    assert _client(api, state).report_environment(SESSION, EnvironmentRecord()) is None
    assert api.environments == []


def test_a_failed_send_is_reported_and_retried_next_time(api: FakeApi, state: StateStore) -> None:
    record = EnvironmentRecord(surface="terminal")
    client = _client(api, state)

    api.fail_with = ApiUnavailableError("down")
    reason = client.report_environment(SESSION, record)
    assert reason is not None and "unavailable" in reason
    assert api.environments == []

    api.fail_with = None  # the API is back: the same record is sent
    assert client.report_environment(SESSION, record) is None
    assert api.environments == [record]


def test_a_refused_environment_is_reported_with_the_reason(api: FakeApi, state: StateStore) -> None:
    api.fail_with = ApiRejectedError(422, "bad record")
    reason = _client(api, state).report_environment(SESSION, EnvironmentRecord(surface="x"))
    assert reason is not None and "422" in reason


def test_the_failure_reason_never_contains_the_values(api: FakeApi, state: StateStore) -> None:
    api.fail_with = ApiUnavailableError("down")
    record = EnvironmentRecord(account_email="dev@example.com", hostname="laptop-1")
    reason = _client(api, state).report_environment(SESSION, record)
    assert "dev@example.com" not in str(reason) and "laptop-1" not in str(reason)


# ---------------------------------------------------------------- built for hooks


def test_a_stream_result_says_when_the_api_was_unavailable(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    api.fail_with = ApiUnavailableError("down")
    (result,) = _client(api, state).sync_session(SESSION, transcript)
    assert result.api_unavailable and result.error is not None

    api.fail_with = ApiRejectedError(401, "bad token")  # a refused token is not "unavailable"
    (refused,) = _client(api, state).sync_session(SESSION, transcript)
    assert not refused.api_unavailable and refused.access_denied and refused.error is not None


def test_sub_agent_streams_are_not_tried_once_the_api_is_known_to_be_down(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, _entry(1))
    folder = transcript.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    for name in ("agent-a", "agent-b"):
        _write(folder / f"{name}.jsonl", {"type": "user", "uuid": name})
    api.fail_with = ApiUnavailableError("down")

    results = _client(api, state).sync_session(SESSION, transcript)

    assert [r.stream for r in results] == ["main"]  # the others would only have failed slowly too


def test_a_call_can_use_its_own_time_limit(
    api: FakeApi, state: StateStore, transcript: Path
) -> None:
    _write(transcript, *[_entry(n) for n in range(1, 5)])
    line_size = len(json.dumps(_entry(1), separators=(",", ":"))) + 1
    clock = Clock()
    client = _client(api, state, chunk_bytes=line_size, budget_seconds=1000.0, clock=clock)

    original_put = api.put_chunk

    def slow(session_id: str, stream: str, offset: int, body: bytes) -> bool:
        clock.now += 6.0
        return original_put(session_id, stream, offset, body)

    api.put_chunk = slow  # type: ignore[method-assign]
    (result,) = client.sync_session(
        SESSION, transcript, budget_seconds=10.0
    )  # not the 1000 default
    assert result.chunks == 2 and not result.caught_up


def test_catch_up_stops_at_the_first_unavailable_api(
    api: FakeApi, state: StateStore, tmp_path: Path
) -> None:
    for name in ("s-a", "s-b", "s-c"):
        path = tmp_path / name / f"{name}.jsonl"
        _write(path, _entry(1))
        state.remember_transcript(name, path)
    api.fail_with = ApiUnavailableError("down")

    results = _client(api, state).catch_up()

    assert list(results) == ["s-a"]  # the other sessions were not tried


def test_catch_up_shares_one_time_limit_across_sessions(
    api: FakeApi, state: StateStore, tmp_path: Path
) -> None:
    clock = Clock()
    for name in ("s-a", "s-b", "s-c"):
        path = tmp_path / name / f"{name}.jsonl"
        _write(path, _entry(1))
        state.remember_transcript(name, path)
    original_put = api.put_chunk

    def slow(session_id: str, stream: str, offset: int, body: bytes) -> bool:
        clock.now += 4.0  # each session's upload takes 4 s
        return original_put(session_id, stream, offset, body)

    api.put_chunk = slow  # type: ignore[method-assign]
    results = _client(api, state, clock=clock).catch_up(budget_seconds=6.0)
    assert list(results) == ["s-a", "s-b"]  # time ran out before the third session
