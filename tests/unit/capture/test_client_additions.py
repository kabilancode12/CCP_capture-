"""What a sync sends besides transcripts: lifecycle events, tool-result files, newer fields.

Each of these uses an endpoint that an older server does not have, so each test also checks that
such a server's answer (404 or 405) is logged once and does not stop the transcript upload.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ccp.capture.api import ApiRejectedError, ApiUnavailableError, ToolResultReply
from ccp.capture.client import (
    API_UNAVAILABLE,
    ENVIRONMENT_PARTIAL,
    MAX_PENDING_LIFECYCLE,
    MAX_TOOL_RESULT_BYTES,
    TOKEN_REFUSED,
    UNSUPPORTED_RETRY_SECONDS,
    WORKFLOW_RUN_KEY,
    CaptureClient,
)
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.lifecycle import LifecycleEvent

from .conftest import Clock, FakeApi

SESSION = "sess-1"
KEY = "6f1c2d3e-0000-4000-8000-00000000abcd"
T0 = datetime(2026, 11, 15, 17, 35, tzinfo=UTC)


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "projects" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"user","uuid":"u1"}\n', encoding="utf-8")
    return path


@pytest.fixture
def wall() -> Clock:
    clock = Clock()
    clock.now = 1_000_000.0
    return clock


@pytest.fixture
def client(api: FakeApi, state: StateStore, wall: Clock) -> CaptureClient:
    return CaptureClient(api, state, wall_clock=wall)


def _start(at: datetime = T0, key: str = KEY) -> LifecycleEvent:
    return LifecycleEvent("start", key, at, source="startup", commit="ab" * 20)


def _end(at: datetime = T0 + timedelta(hours=1), key: str = KEY) -> LifecycleEvent:
    return LifecycleEvent("end", key, at, reason="prompt_input_exit")


# ---------------------------------------------------------------- lifecycle events


def test_queued_events_are_sent_oldest_first_and_then_forgotten(
    api: FakeApi, client: CaptureClient
) -> None:
    client.queue_lifecycle(SESSION, _end())
    client.queue_lifecycle(SESSION, _start())

    assert client.send_lifecycle(SESSION) == "2 lifecycle event(s) sent"
    assert [(e.kind, e.run_key, e.at) for e in api.lifecycle] == [
        ("start", KEY, T0),
        ("end", KEY, T0 + timedelta(hours=1)),
    ]
    assert client.send_lifecycle(SESSION) is None  # nothing waits any more


def test_events_wait_while_the_api_is_down(api: FakeApi, client: CaptureClient) -> None:
    client.queue_lifecycle(SESSION, _start())
    api.fail_with = ApiUnavailableError("down")
    assert client.send_lifecycle(SESSION) == API_UNAVAILABLE

    api.fail_with = None
    assert client.send_lifecycle(SESSION) == "1 lifecycle event(s) sent"
    assert len(api.lifecycle) == 1


@pytest.mark.parametrize("status", [404, 405])
def test_an_older_server_is_asked_again_only_after_an_hour(
    status: int, api: FakeApi, client: CaptureClient, wall: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ccp.capture")
    client.queue_lifecycle(SESSION, _start())
    api.fail_with = ApiRejectedError(status, "not found")

    assert "does not record them yet" in (client.send_lifecycle(SESSION) or "")
    assert "does not record them yet" in (client.send_lifecycle(SESSION) or "")
    assert [kind for kind, _ in api.calls] == []  # the second time nothing was asked
    assert sum("does not offer lifecycle" in r.message for r in caplog.records) == 1

    api.fail_with = None
    wall.now += UNSUPPORTED_RETRY_SECONDS + 1
    assert client.send_lifecycle(SESSION) == "1 lifecycle event(s) sent"  # kept, sent after all


def test_an_event_the_server_refuses_as_malformed_is_dropped(
    api: FakeApi, client: CaptureClient
) -> None:
    client.queue_lifecycle(SESSION, _start())
    api.fail_with = ApiRejectedError(422, "bad")
    client.send_lifecycle(SESSION)
    api.fail_with = None
    assert client.send_lifecycle(SESSION) is None


def test_a_refused_token_keeps_the_events(api: FakeApi, client: CaptureClient) -> None:
    client.queue_lifecycle(SESSION, _start())
    api.fail_with = ApiRejectedError(401, "no")
    assert client.send_lifecycle(SESSION) == TOKEN_REFUSED
    api.fail_with = None
    assert client.send_lifecycle(SESSION) == "1 lifecycle event(s) sent"


def test_only_the_newest_events_are_kept_when_too_many_wait(
    api: FakeApi, client: CaptureClient
) -> None:
    for minute in range(MAX_PENDING_LIFECYCLE + 5):
        client.queue_lifecycle(SESSION, _start(T0 + timedelta(minutes=minute), f"key-{minute:05d}"))
    client.send_lifecycle(SESSION)
    assert len(api.lifecycle) == MAX_PENDING_LIFECYCLE
    assert api.lifecycle[0].run_key == "key-00005"


def test_an_unreadable_queued_event_is_removed(
    api: FakeApi, client: CaptureClient, state: StateStore
) -> None:
    state.set_marker(SESSION, "lifecycle-start-broken", "{not json")
    assert client.send_lifecycle(SESSION) is None
    assert state.marker_names(SESSION, "lifecycle-") == []


def test_a_sync_sends_waiting_events_after_the_transcript(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    client.queue_lifecycle(SESSION, _start())
    client.sync_session(SESSION, transcript)
    assert [kind for kind, _ in api.calls] == ["chunk", "lifecycle"]


def test_catch_up_sends_events_of_chats_without_a_transcript(
    api: FakeApi, client: CaptureClient
) -> None:
    # Most chats opened in VS Code are closed without a message: they only have lifecycle events.
    client.queue_lifecycle("empty-chat", _start())
    client.queue_lifecycle("empty-chat", _end())
    assert client.catch_up() == {}
    assert [e.kind for e in api.lifecycle] == ["start", "end"]


@pytest.mark.parametrize(
    "error", [ApiUnavailableError("down"), ApiRejectedError(401, "no")], ids=["down", "token"]
)
def test_catch_up_stops_at_the_first_chat_whose_events_cannot_be_sent(
    error: Exception, api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    # Every other request would fail the same way: one refused or timed-out request is enough.
    client.sync_session(SESSION, transcript)  # a known chat with a transcript
    for chat in ("chat-a", "chat-b"):
        client.queue_lifecycle(chat, _start())
    api.attempts = 0
    api.fail_with = error

    assert client.catch_up() == {}  # the known transcript was not tried either
    assert api.attempts == 1


def test_an_older_server_does_not_stop_the_transcript_upload(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    client.queue_lifecycle(SESSION, _start())
    api.fail_with, api.fail_on = ApiRejectedError(404, "not found"), "lifecycle"
    (result,) = client.sync_session(SESSION, transcript)
    assert (result.chunks, result.error) == (1, None)


# ---------------------------------------------------------------- tool-result files


def _tool_results(transcript: Path) -> Path:
    folder = transcript.with_suffix("") / "tool-results"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_tool_result_files_are_redacted_uploaded_and_reported(
    api: FakeApi, client: CaptureClient, transcript: Path, fakes: dict[str, str]
) -> None:
    folder = _tool_results(transcript)
    (folder / "bs12jynbi.txt").write_text(f"output\nmail {fakes['email']}\n", encoding="utf-8")

    results = client.sync_session(SESSION, transcript)

    assert results[-1].stream == "tool-results" and results[-1].files == 1
    body = api.tool_results["bs12jynbi.txt"].decode()
    assert fakes["email"] not in body and "[REDACTED:email]" in body
    assert [(r.rule_id, r.location) for r in api.redactions] == [
        ("email", f"sessions/{SESSION}/tool-results/bs12jynbi.txt:0:")
    ]


def test_an_unchanged_file_is_not_read_or_sent_again_but_a_changed_one_is(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    path = _tool_results(transcript) / "b1.txt"
    path.write_text("first\n", encoding="utf-8")
    client.sync_session(SESSION, transcript)
    api.calls.clear()

    client.sync_session(SESSION, transcript)
    assert api.calls == []

    path.write_text("first\nand more\n", encoding="utf-8")
    client.sync_session(SESSION, transcript)
    assert api.calls == [("tool-result", "b1.txt")]
    assert api.tool_results["b1.txt"] == b"first\nand more\n"


def test_a_file_too_large_or_with_an_unusable_name_is_skipped(
    api: FakeApi, client: CaptureClient, transcript: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = _tool_results(transcript)
    with (folder / "big.txt").open("wb") as handle:
        handle.truncate(MAX_TOOL_RESULT_BYTES + 1)
    (folder / "bad name.txt").write_text("x", encoding="utf-8")
    (folder / "looks-like-a-chunk.jsonl").write_text("{}", encoding="utf-8")

    results = client.sync_session(SESSION, transcript)
    assert api.tool_results == {} and results[-1].caught_up and results[-1].error is None
    assert sum("unusable name" in r.message for r in caplog.records) == 2

    caplog.clear()
    client.sync_session(SESSION, transcript)
    assert caplog.records == []  # remembered: not looked at (or logged) again at every sync


def test_the_prompt_hook_can_leave_tool_results_for_later(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    (_tool_results(transcript) / "b1.txt").write_text("x", encoding="utf-8")
    results = client.sync_session(SESSION, transcript, tool_results=False)
    assert [r.stream for r in results] == ["main"] and api.tool_results == {}
    client.sync_session(SESSION, transcript)
    assert api.tool_results == {"b1.txt": b"x"}


def test_redactions_of_a_changed_file_name_the_version_the_server_stored(
    api: FakeApi, client: CaptureClient, transcript: Path, fakes: dict[str, str]
) -> None:
    path = _tool_results(transcript) / "b1.txt"
    path.write_text("first\n", encoding="utf-8")
    client.sync_session(SESSION, transcript)
    path.write_text(f"first\nmail {fakes['email']}\n", encoding="utf-8")

    client.sync_session(SESSION, transcript)

    (record,) = api.redactions
    base = f"sessions/{SESSION}/tool-results/b1.txt"
    assert record.location.startswith(base + "@") and record.location.endswith(":0:")


def test_a_reply_without_a_usable_key_reports_against_the_files_own_key(
    api: FakeApi, state: StateStore, transcript: Path, fakes: dict[str, str]
) -> None:
    class ElsewhereApi(FakeApi):
        def put_tool_result(self, session_id: str, name: str, body: bytes) -> ToolResultReply:
            return ToolResultReply(True, "sessions/other/tool-results/x.txt")

    other = ElsewhereApi()
    (_tool_results(transcript) / "b1.txt").write_text(fakes["email"], encoding="utf-8")
    CaptureClient(other, state).sync_session(SESSION, transcript)
    assert [r.location for r in other.redactions] == [f"sessions/{SESSION}/tool-results/b1.txt:0:"]


def test_an_older_server_is_not_asked_for_tool_results_again_for_an_hour(
    api: FakeApi, client: CaptureClient, transcript: Path, wall: Clock
) -> None:
    (_tool_results(transcript) / "b1.txt").write_text("x", encoding="utf-8")
    api.fail_with, api.fail_on = ApiRejectedError(404, "not found"), "tool-result"
    (main, tool_results) = client.sync_session(SESSION, transcript)
    assert main.chunks == 1 and tool_results.error is None and not tool_results.stops_run

    api.calls.clear()
    client.sync_session(SESSION, transcript)
    assert api.calls == []

    api.fail_with = None
    wall.now += UNSUPPORTED_RETRY_SECONDS + 1
    client.sync_session(SESSION, transcript)
    assert api.tool_results == {"b1.txt": b"x"}


def test_a_refused_tool_result_never_counts_as_a_chunk_refusal(
    api: FakeApi, client: CaptureClient, transcript: Path, state: StateStore
) -> None:
    (_tool_results(transcript) / "b1.txt").write_text("x", encoding="utf-8")
    api.fail_with, api.fail_on = ApiRejectedError(413, "too large"), "tool-result"
    client.sync_session(SESSION, transcript)
    assert state.marker_names(SESSION, "refused-") == []
    api.calls.clear()
    client.sync_session(SESSION, transcript)
    assert api.calls == []  # given up for this version of the file


def test_a_secret_escaped_inside_a_json_file_is_redacted(
    api: FakeApi, client: CaptureClient, transcript: Path, fakes: dict[str, str]
) -> None:
    # In the raw text the token follows the two characters "\\n", which hides it from the rule;
    # decoded, it follows a real line break, as it would in a transcript line.
    token = fakes["github-token"]
    (_tool_results(transcript) / "b1.json").write_text(
        json.dumps({"output": "line\n" + token}), encoding="utf-8"
    )

    client.sync_session(SESSION, transcript)

    stored = json.loads(api.tool_results["b1.json"])
    assert stored == {"output": "line\n[REDACTED:github-token]"}
    assert [r.rule_id for r in api.redactions] == ["github-token"]


def test_a_json_file_without_secrets_keeps_its_layout(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    text = '{"a":1,  "b": [2,3]}\n'
    (_tool_results(transcript) / "b1.json").write_text(text, encoding="utf-8")
    client.sync_session(SESSION, transcript)
    assert api.tool_results["b1.json"] == text.encode()


def test_the_flush_adds_the_commit_to_the_newest_waiting_end(
    api: FakeApi, client: CaptureClient
) -> None:
    client.queue_lifecycle(SESSION, _start())
    client.queue_lifecycle(SESSION, _end())

    assert client.add_end_commit(SESSION, "cd" * 20)
    assert not client.add_end_commit(SESSION, "ef" * 20)  # it has one now
    assert not client.add_end_commit(SESSION, None)
    client.send_lifecycle(SESSION)

    assert [(e.kind, e.commit) for e in api.lifecycle] == [("start", "ab" * 20), ("end", "cd" * 20)]


def test_tool_results_wait_while_the_api_is_down(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    (_tool_results(transcript) / "b1.txt").write_text("x", encoding="utf-8")
    api.fail_with, api.fail_on = ApiUnavailableError("down"), "tool-result"
    results = client.sync_session(SESSION, transcript)
    assert results[-1].api_unavailable

    api.fail_with = None
    client.sync_session(SESSION, transcript)
    assert api.tool_results == {"b1.txt": b"x"}


# ---------------------------------------------------------------- the environment record


class OldApi(FakeApi):
    """A server from before migration 016: it refuses the newer environment fields."""

    def put_environment(self, session_id: str, record: EnvironmentRecord) -> None:
        if record.account_uuid or record.folder:
            raise ApiRejectedError(422, "2 unknown field(s)")
        super().put_environment(session_id, record)


def test_an_older_server_gets_the_fields_it_knows(state: StateStore) -> None:
    old = OldApi()
    record = EnvironmentRecord(surface="terminal", account_uuid="u-1", folder="/work/app")

    assert CaptureClient(old, state).report_environment(SESSION, record) == ENVIRONMENT_PARTIAL
    assert old.environments == [EnvironmentRecord(surface="terminal")]


def test_a_record_sent_in_part_is_sent_whole_once_the_server_knows_the_fields(
    state: StateStore,
) -> None:
    record = EnvironmentRecord(surface="terminal", account_uuid="u-1", folder="/work/app")
    CaptureClient(OldApi(), state).report_environment(SESSION, record)

    upgraded = FakeApi()
    assert CaptureClient(upgraded, state).report_environment(SESSION, record) is None
    assert upgraded.environments == [record]  # what was sent in part is not taken as sent
    assert CaptureClient(upgraded, state).report_environment(SESSION, record) is None
    assert upgraded.environments == [record]  # and once it was sent whole, it is not sent again


def test_a_record_of_only_newer_fields_is_refused_as_it_is(
    api: FakeApi, client: CaptureClient
) -> None:
    api.fail_with = ApiRejectedError(422, "unknown field")
    reason = client.report_environment(SESSION, EnvironmentRecord(account_uuid="u-1"))
    assert reason is not None and "422" in reason


# ---------------------------------------------------------------- Workflow sub-agents


def test_the_workflow_run_id_is_added_to_a_workflow_agent_description(
    api: FakeApi, client: CaptureClient, transcript: Path
) -> None:
    run = transcript.with_suffix("") / "subagents" / "workflows" / "wf_15ed621d-87a"
    run.mkdir(parents=True)
    (run / "agent-a1.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    (run / "agent-a1.meta.json").write_text('{"agentType":"workflow-subagent"}', encoding="utf-8")
    direct = transcript.with_suffix("") / "subagents"
    (direct / "agent-b2.meta.json").write_text('{"agentType":"Explore"}', encoding="utf-8")

    client.sync_session(SESSION, transcript)

    workflow_meta = json.loads(api.chunks[("agent-a1.meta", 0)])
    assert workflow_meta == {"agentType": "workflow-subagent", WORKFLOW_RUN_KEY: "wf_15ed621d-87a"}
    assert json.loads(api.chunks[("agent-b2.meta", 0)]) == {"agentType": "Explore"}
