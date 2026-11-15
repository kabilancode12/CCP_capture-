import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ccp.capture.client import ENVIRONMENT_PARTIAL, UNSUPPORTED_RETRY_SECONDS, StreamResult
from ccp.capture.hooks import (
    COMPACT_BUDGET_SECONDS,
    ENVIRONMENT_COMPLETE_MARKER,
    PROMPT_BUDGET_SECONDS,
    RUN_KEY_MARKER,
    SESSION_END,
    SESSION_START,
    START_BUDGET_SECONDS,
    START_COMMIT_TIMEOUT_SECONDS,
    STOP,
    STOP_BUDGET_SECONDS,
    SUBAGENT_BUDGET_SECONDS,
    HookHandler,
    HookPayload,
    parse_payload,
)
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord
from ccp.lifecycle import LifecycleEvent

SESSION = "sess-1"
MOMENT = datetime(2026, 11, 15, 17, 35, tzinfo=UTC)


class RecordingClient:
    """Remembers how the handler used it; stands in for the real capture client."""

    def __init__(self) -> None:
        self.synced: list[tuple[str, float | None]] = []
        self.tool_results: list[bool] = []  # whether each sync included the tool-result files
        self.caught_up: list[float | None] = []
        self.environments: list[EnvironmentRecord] = []
        self.environment_problem: str | None = None
        self.sync_results = [StreamResult("main", chunks=2, attachments=1)]
        self.queued: list[LifecycleEvent] = []
        self.lifecycle_sends: list[str] = []

    def sync_session(
        self,
        session_id: str,
        transcript_path: Path,
        budget_seconds: float | None = None,
        *,
        tool_results: bool = True,
    ) -> list[StreamResult]:
        self.synced.append((session_id, budget_seconds))
        self.tool_results.append(tool_results)
        return self.sync_results

    def catch_up(self, budget_seconds: float | None = None) -> dict[str, list[StreamResult]]:
        self.caught_up.append(budget_seconds)
        return {"older-session": []}

    def report_environment(self, session_id: str, record: EnvironmentRecord) -> str | None:
        self.environments.append(record)
        return self.environment_problem

    def queue_lifecycle(self, session_id: str, event: LifecycleEvent) -> None:
        self.queued.append(event)

    def send_lifecycle(self, session_id: str) -> str | None:
        self.lifecycle_sends.append(session_id)
        return "1 lifecycle event(s) sent"


Flushed = list[tuple[str, Path | None, Path | None]]


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def flushed() -> Flushed:
    return []


@pytest.fixture
def commits() -> list[tuple[Path, float]]:
    """Every request for the Git HEAD (folder and time limit)."""
    return []


@pytest.fixture
def handler(
    client: RecordingClient,
    tmp_path: Path,
    flushed: Flushed,
    commits: list[tuple[Path, float]],
) -> HookHandler:
    def collect(transcript: Path | None, cwd: Path | None) -> EnvironmentRecord:
        version = "2.1.289" if transcript is not None else None
        folder = str(cwd) if cwd is not None else None
        return EnvironmentRecord(surface="terminal", claude_code_version=version, folder=folder)

    def read_commit(folder: Path, timeout: float) -> str | None:
        commits.append((folder, timeout))
        return "a" * 40

    return HookHandler(
        client,
        StateStore(tmp_path / "state"),
        collect,
        lambda session_id, transcript, cwd: flushed.append((session_id, transcript, cwd)),
        read_commit=read_commit,
        clock=lambda: MOMENT,
    )


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def _payload(
    event: str, transcript: Path | None, cwd: Path | None = None, **extra: str | None
) -> HookPayload:
    return HookPayload(
        event=event,
        session_id=SESSION,
        transcript=transcript,
        agent_type=extra.get("agent_type"),
        source=extra.get("source"),
        cwd=cwd,
        reason=extra.get("reason"),
    )


# ---------------------------------------------------------------- reading the payload


def test_a_real_payload_is_parsed(tmp_path: Path) -> None:
    text = json.dumps(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "abc",
            "transcript_path": str(tmp_path / "abc.jsonl"),
            "agent_type": "Explore",
            "source": "startup",
            "prompt": "never used",
        }
    )
    payload = parse_payload(text)
    assert payload == HookPayload(
        "SubagentStop", "abc", tmp_path / "abc.jsonl", agent_type="Explore", source="startup"
    )


def test_the_end_reason_and_working_folder_are_read() -> None:
    text = json.dumps(
        {"hook_event_name": "SessionEnd", "session_id": "abc", "reason": "logout", "cwd": "/w"}
    )
    payload = parse_payload(text)
    assert payload is not None and (payload.reason, payload.cwd) == ("logout", Path("/w"))
    assert parse_payload(json.dumps({**json.loads(text), "reason": 5})).reason is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[1]", "{}", '{"hook_event_name":"Stop"}', '{"session_id":"s"}',
     '{"hook_event_name":"Stop","session_id":""}', '{"hook_event_name":5,"session_id":"s"}'],
)  # fmt: skip
def test_unusable_payloads_give_none(text: str) -> None:
    assert parse_payload(text) is None


def test_only_a_jsonl_path_is_accepted_as_the_transcript() -> None:
    base = {"hook_event_name": "Stop", "session_id": "s"}
    assert parse_payload(json.dumps({**base, "transcript_path": "/x/notes.txt"})).transcript is None  # type: ignore[union-attr]
    assert parse_payload(json.dumps({**base, "transcript_path": 5})).transcript is None  # type: ignore[union-attr]
    assert parse_payload(json.dumps(base)).transcript is None  # type: ignore[union-attr]


# ---------------------------------------------------------------- one event at a time


def test_session_start_records_the_environment_catches_up_and_syncs_the_session(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    outcome = handler.handle(_payload(SESSION_START, transcript, source="resume"))

    assert [r.surface for r in client.environments] == ["terminal"]
    assert client.caught_up == [START_BUDGET_SECONDS]
    assert client.synced == [
        (SESSION, START_BUDGET_SECONDS)
    ]  # a resumed session already has a file
    assert "source=resume" in outcome and "caught up 1 earlier session(s)" in outcome


def test_session_start_of_a_new_session_has_no_transcript_to_sync(
    handler: HookHandler, client: RecordingClient, tmp_path: Path
) -> None:
    handler.handle(_payload(SESSION_START, tmp_path / "not-yet.jsonl", source="startup"))
    assert client.synced == []  # the file does not exist yet (observed on Claude Code 2.1.289)
    assert client.caught_up == [START_BUDGET_SECONDS]
    assert client.environments[0].claude_code_version is None  # only the process stage is known


def test_session_start_runs_again_after_compact_without_harm(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    handler.handle(_payload(SESSION_START, transcript, source="startup"))
    handler.handle(_payload(SESSION_START, transcript, source="compact"))  # same session again
    assert len(client.environments) == 1  # the record was already complete: not collected again


@pytest.mark.parametrize(
    ("event", "budget"),
    [
        ("UserPromptSubmit", PROMPT_BUDGET_SECONDS),
        (STOP, STOP_BUDGET_SECONDS),
        ("PreCompact", COMPACT_BUDGET_SECONDS),
    ],
)
def test_these_events_sync_the_session_with_their_own_time_limit(
    handler: HookHandler, client: RecordingClient, transcript: Path, event: str, budget: float
) -> None:
    outcome = handler.handle(_payload(event, transcript))
    assert client.synced == [(SESSION, budget)]
    assert "2 chunk(s), 1 attachment(s) uploaded" in outcome


def test_the_prompt_hook_leaves_tool_result_files_for_a_later_sync(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    # One large file could take longer than the prompt hook's time limit.
    handler.handle(_payload("UserPromptSubmit", transcript))
    handler.handle(_payload(STOP, transcript))
    assert client.tool_results == [False, True]


def test_a_hook_that_holds_claude_up_gets_a_shorter_time_limit_than_stop() -> None:
    assert PROMPT_BUDGET_SECONDS < STOP_BUDGET_SECONDS
    assert START_BUDGET_SECONDS < STOP_BUDGET_SECONDS


def test_an_event_without_a_transcript_does_nothing_yet(
    handler: HookHandler, client: RecordingClient, tmp_path: Path
) -> None:
    assert handler.handle(_payload(STOP, tmp_path / "missing.jsonl")) == "no transcript yet"
    assert handler.handle(_payload(STOP, None)) == "no transcript yet"
    assert client.synced == []


def test_a_real_sub_agent_stop_syncs_the_session(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    handler.handle(_payload("SubagentStop", transcript, agent_type="Explore"))
    assert client.synced == [(SESSION, SUBAGENT_BUDGET_SECONDS)]


def test_an_internal_helper_stop_is_skipped(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    # Observed: Claude Code fires SubagentStop after almost every turn for an internal helper with
    # an empty agent type and no transcript. Syncing for each would only add delay.
    for agent_type in (None, ""):
        outcome = handler.handle(_payload("SubagentStop", transcript, agent_type=agent_type))
        assert outcome.startswith("skipped")
    assert client.synced == []


def test_session_end_starts_a_background_flush_and_does_not_upload_itself(
    handler: HookHandler,
    client: RecordingClient,
    transcript: Path,
    flushed: Flushed,
) -> None:
    outcome = handler.handle(_payload(SESSION_END, transcript))
    assert flushed == [(SESSION, transcript, None)]  # this payload has no working directory
    assert client.synced == []  # SessionEnd has almost no time: nothing is waited for
    assert client.lifecycle_sends == []  # the flush sends the end, not the hook
    assert outcome == "end queued; flush started in the background"


def test_session_end_survives_a_flush_that_cannot_be_started(
    client: RecordingClient, tmp_path: Path, transcript: Path
) -> None:
    def fail(session_id: str, path: Path | None, cwd: Path | None) -> None:
        raise PermissionError("blocked")

    handler = HookHandler(
        client, StateStore(tmp_path / "s"), lambda t, c: EnvironmentRecord(), fail
    )
    assert "could not be started" in handler.handle(_payload(SESSION_END, transcript))
    assert len(client.queued) == 1  # the end waits for the next catch-up


def test_session_end_without_a_transcript_still_sends_the_end(
    handler: HookHandler, client: RecordingClient, flushed: Flushed, tmp_path: Path
) -> None:
    # Observed: most chats opened in VS Code are closed without a message and have no transcript.
    outcome = handler.handle(_payload(SESSION_END, tmp_path / "none.jsonl", reason="other"))
    assert flushed == [(SESSION, None, None)]
    assert [(e.kind, e.reason) for e in client.queued] == [("end", "other")]
    assert "no transcript" in outcome


# ---------------------------------------------------------------- session start and end


def test_session_start_queues_a_start_with_a_new_run_key_for_the_catch_up_to_send(
    handler: HookHandler,
    client: RecordingClient,
    tmp_path: Path,
    commits: list[tuple[Path, float]],
) -> None:
    outcome = handler.handle(_payload(SESSION_START, None, cwd=tmp_path, source="resume"))

    (start,) = client.queued
    assert (start.kind, start.source, start.at, start.commit) == (
        "start",
        "resume",
        MOMENT,
        "a" * 40,
    )
    assert StateStore(tmp_path / "state").get_marker(SESSION, RUN_KEY_MARKER) == start.run_key
    # Not sent on its own (one more request that could wait for the timeout): the catch-up sends it.
    assert client.lifecycle_sends == [] and client.caught_up == [START_BUDGET_SECONDS]
    assert commits == [(tmp_path, START_COMMIT_TIMEOUT_SECONDS)]
    assert "start queued" in outcome


def test_the_end_carries_the_key_of_its_start_and_forgets_it(
    handler: HookHandler, client: RecordingClient, tmp_path: Path, commits: list[tuple[Path, float]]
) -> None:
    handler.handle(_payload(SESSION_START, None, cwd=tmp_path, source="startup"))
    handler.handle(_payload(SESSION_END, None, cwd=tmp_path, reason="prompt_input_exit"))

    start, end = client.queued
    assert (end.kind, end.reason, end.run_key) == ("end", "prompt_input_exit", start.run_key)
    assert StateStore(tmp_path / "state").get_marker(SESSION, RUN_KEY_MARKER) is None


def test_session_end_does_not_wait_for_git(
    handler: HookHandler, client: RecordingClient, tmp_path: Path, commits: list[tuple[Path, float]]
) -> None:
    # SessionEnd may be stopped at any moment: the detached flush adds the commit to the end.
    handler.handle(_payload(SESSION_END, None, cwd=tmp_path, reason="other"))
    assert commits == [] and client.queued[0].commit is None


def test_an_end_without_a_known_start_gets_a_fresh_key(
    handler: HookHandler, client: RecordingClient
) -> None:
    handler.handle(_payload(SESSION_END, None, reason="logout"))
    handler.handle(_payload(SESSION_END, None, reason="logout"))
    first, second = client.queued
    assert first.run_key != second.run_key and len(first.run_key) == 36


def test_every_start_is_a_new_session_of_the_chat(
    handler: HookHandler, client: RecordingClient
) -> None:
    handler.handle(_payload(SESSION_START, None, source="startup"))
    handler.handle(_payload(SESSION_START, None, source="compact"))
    assert len({event.run_key for event in client.queued}) == 2


def test_without_a_working_folder_no_commit_is_read(
    handler: HookHandler, client: RecordingClient, commits: list[tuple[Path, float]]
) -> None:
    handler.handle(_payload(SESSION_START, None, source="startup"))
    assert commits == [] and client.queued[0].commit is None


def test_the_environment_is_given_the_working_folder(
    handler: HookHandler, client: RecordingClient, tmp_path: Path
) -> None:
    handler.handle(_payload(SESSION_START, None, cwd=tmp_path, source="startup"))
    assert client.environments[0].folder == str(tmp_path)


def test_an_event_the_client_does_not_handle_is_ignored(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    assert handler.handle(_payload("Notification", transcript)).startswith("ignored")
    assert client.synced == [] and client.caught_up == []


# ---------------------------------------------------------------- the environment record


def test_the_environment_is_collected_until_it_is_complete_then_left_alone(
    handler: HookHandler, client: RecordingClient, tmp_path: Path, transcript: Path
) -> None:
    handler.handle(_payload(SESSION_START, tmp_path / "later.jsonl", source="startup"))
    assert client.environments[-1].claude_code_version is None  # incomplete: sent without version
    handler.handle(_payload(STOP, transcript))  # the transcript exists: the record is completed
    assert client.environments[-1].claude_code_version == "2.1.289"
    handler.handle(_payload(STOP, transcript))
    handler.handle(_payload("UserPromptSubmit", transcript))
    assert len(client.environments) == 2  # nothing more to collect or send


def test_a_failed_environment_send_is_reported_and_retried_at_the_next_hook(
    handler: HookHandler, client: RecordingClient, transcript: Path, tmp_path: Path
) -> None:
    client.environment_problem = "the API is unavailable; will retry on the next sync"
    outcome = handler.handle(_payload(STOP, transcript))
    assert "environment: not sent (the API is unavailable" in outcome
    assert StateStore(tmp_path / "state").get_marker(SESSION, ENVIRONMENT_COMPLETE_MARKER) is None

    client.environment_problem = None
    handler.handle(_payload(STOP, transcript))
    assert len(client.environments) == 2  # tried again


def test_upload_errors_appear_in_the_outcome_without_content(
    handler: HookHandler, client: RecordingClient, transcript: Path
) -> None:
    client.sync_results = [StreamResult("main", error="the API is unavailable; will retry")]
    outcome = handler.handle(_payload(STOP, transcript))
    assert "main: the API is unavailable; will retry" in outcome


def test_a_record_an_older_api_took_in_part_is_sent_again_an_hour_later(
    client: RecordingClient, tmp_path: Path, transcript: Path
) -> None:
    moment = [MOMENT]
    handler = HookHandler(
        client,
        StateStore(tmp_path / "state"),
        lambda t, c: EnvironmentRecord(claude_code_version="2.1.325", folder="/work"),
        lambda session_id, transcript, cwd: None,
        clock=lambda: moment[0],
    )
    client.environment_problem = ENVIRONMENT_PARTIAL

    assert "environment: sent in part" in handler.handle(_payload(STOP, transcript))
    assert "recorded in part" in handler.handle(_payload(STOP, transcript))
    assert len(client.environments) == 1  # not collected or sent again within the hour

    moment[0] = MOMENT + timedelta(seconds=UNSUPPORTED_RETRY_SECONDS)
    client.environment_problem = None  # the server was upgraded meanwhile
    assert "environment: sent" in handler.handle(_payload(STOP, transcript))
    assert len(client.environments) == 2
    marker = StateStore(tmp_path / "state").get_marker(SESSION, ENVIRONMENT_COMPLETE_MARKER)
    assert marker == "1"
