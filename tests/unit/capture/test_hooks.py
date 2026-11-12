import json
from pathlib import Path

import pytest

from ccp.capture.client import StreamResult
from ccp.capture.hooks import (
    COMPACT_BUDGET_SECONDS,
    ENVIRONMENT_COMPLETE_MARKER,
    PROMPT_BUDGET_SECONDS,
    SESSION_END,
    SESSION_START,
    START_BUDGET_SECONDS,
    STOP,
    STOP_BUDGET_SECONDS,
    SUBAGENT_BUDGET_SECONDS,
    HookHandler,
    HookPayload,
    parse_payload,
)
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord

SESSION = "sess-1"


class RecordingClient:
    """Remembers how the handler used it; stands in for the real capture client."""

    def __init__(self) -> None:
        self.synced: list[tuple[str, float | None]] = []
        self.caught_up: list[float | None] = []
        self.environments: list[EnvironmentRecord] = []
        self.environment_problem: str | None = None
        self.sync_results = [StreamResult("main", chunks=2, attachments=1)]

    def sync_session(
        self, session_id: str, transcript_path: Path, budget_seconds: float | None = None
    ) -> list[StreamResult]:
        self.synced.append((session_id, budget_seconds))
        return self.sync_results

    def catch_up(self, budget_seconds: float | None = None) -> dict[str, list[StreamResult]]:
        self.caught_up.append(budget_seconds)
        return {"older-session": []}

    def report_environment(self, session_id: str, record: EnvironmentRecord) -> str | None:
        self.environments.append(record)
        return self.environment_problem


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def flushed() -> list[tuple[str, Path]]:
    return []


@pytest.fixture
def handler(
    client: RecordingClient, tmp_path: Path, flushed: list[tuple[str, Path]]
) -> HookHandler:
    def collect(transcript: Path | None) -> EnvironmentRecord:
        version = "2.1.289" if transcript is not None else None
        return EnvironmentRecord(surface="terminal", claude_code_version=version)

    return HookHandler(
        client,
        StateStore(tmp_path / "state"),
        collect,
        lambda session_id, transcript: flushed.append((session_id, transcript)),
    )


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def _payload(event: str, transcript: Path | None, **extra: str | None) -> HookPayload:
    return HookPayload(
        event=event,
        session_id=SESSION,
        transcript=transcript,
        agent_type=extra.get("agent_type"),
        source=extra.get("source"),
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
    flushed: list[tuple[str, Path]],
) -> None:
    outcome = handler.handle(_payload(SESSION_END, transcript))
    assert flushed == [(SESSION, transcript)]
    assert client.synced == []  # SessionEnd has almost no time: nothing is waited for
    assert outcome == "flush started in the background"


def test_session_end_survives_a_flush_that_cannot_be_started(
    client: RecordingClient, tmp_path: Path, transcript: Path
) -> None:
    def fail(session_id: str, path: Path) -> None:
        raise PermissionError("blocked")

    handler = HookHandler(client, StateStore(tmp_path / "s"), lambda t: EnvironmentRecord(), fail)
    assert "could not be started" in handler.handle(_payload(SESSION_END, transcript))


def test_session_end_without_a_transcript_has_nothing_to_flush(
    handler: HookHandler, flushed: list[tuple[str, Path]], tmp_path: Path
) -> None:
    assert (
        handler.handle(_payload(SESSION_END, tmp_path / "none.jsonl")) == "no transcript to flush"
    )
    assert flushed == []


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
