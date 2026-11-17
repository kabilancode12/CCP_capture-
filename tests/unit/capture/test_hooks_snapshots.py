"""How the hook handler uses the code-snapshot service."""

import json
from pathlib import Path

import pytest

from ccp.capture.hooks import HookHandler, HookPayload, parse_payload
from ccp.capture.snapshots.context import SnapshotContext
from ccp.capture.state import StateStore
from ccp.environment import EnvironmentRecord

from .test_hooks import SESSION, RecordingClient


class RecordingSnapshots:
    """Remembers how the handler used it; stands in for the snapshot service."""

    def __init__(self, note: str | None = "snapshot 0001, pushed") -> None:
        self.calls: list[tuple[str, str, Path, str | None]] = []
        self.contexts: list[SnapshotContext | None] = []
        self.note = note

    def start(
        self, session_id: str, cwd: Path, context: SnapshotContext | None = None
    ) -> str | None:
        self.calls.append(("start", session_id, cwd, None))
        self.contexts.append(context)
        return self.note

    def snapshot(
        self, session_id: str, cwd: Path, event: str, context: SnapshotContext | None = None
    ) -> str | None:
        self.calls.append(("snapshot", session_id, cwd, event))
        self.contexts.append(context)
        return self.note


@pytest.fixture
def snapshots() -> RecordingSnapshots:
    return RecordingSnapshots()


@pytest.fixture
def client() -> RecordingClient:
    return RecordingClient()


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "repo"
    folder.mkdir()
    return folder


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / f"{SESSION}.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def _handler(
    client: RecordingClient,
    snapshots: RecordingSnapshots | None,
    tmp_path: Path,
    flushed: list[tuple[str, Path | None, Path | None]] | None = None,
) -> HookHandler:
    flushes = flushed if flushed is not None else []
    return HookHandler(
        client,
        StateStore(tmp_path / "state"),
        lambda transcript, cwd: EnvironmentRecord(surface="terminal"),
        lambda session_id, path, cwd: flushes.append((session_id, path, cwd)),
        snapshots,
    )


def _payload(event: str, transcript: Path | None, cwd: Path | None, **extra: str) -> HookPayload:
    return HookPayload(
        event=event,
        session_id=SESSION,
        transcript=transcript,
        agent_type=extra.get("agent_type"),
        source=extra.get("source"),
        cwd=cwd,
    )


# ---------------------------------------------------------------- the payload


def test_the_working_directory_is_read_from_the_payload(tmp_path: Path) -> None:
    text = json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": str(tmp_path)})
    payload = parse_payload(text)
    assert payload is not None and payload.cwd == tmp_path


@pytest.mark.parametrize("cwd", [None, 5, ""])
def test_a_missing_or_invalid_working_directory_is_none(cwd: object) -> None:
    text = json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": cwd})
    payload = parse_payload(text)
    assert payload is not None and payload.cwd is None


# ---------------------------------------------------------------- when snapshots are taken


def test_a_snapshot_follows_the_upload_after_each_response(
    client: RecordingClient,
    snapshots: RecordingSnapshots,
    transcript: Path,
    work_dir: Path,
    tmp_path: Path,
) -> None:
    outcome = _handler(client, snapshots, tmp_path).handle(_payload("Stop", transcript, work_dir))

    assert client.synced and snapshots.calls == [("snapshot", SESSION, work_dir, "Stop")]
    assert outcome.endswith("; code: snapshot 0001, pushed")


def test_the_snapshot_still_happens_if_the_transcript_is_not_there_yet(
    client: RecordingClient, snapshots: RecordingSnapshots, work_dir: Path, tmp_path: Path
) -> None:
    outcome = _handler(client, snapshots, tmp_path).handle(
        _payload("Stop", tmp_path / "missing.jsonl", work_dir)
    )
    assert client.synced == []  # nothing to upload...
    assert snapshots.calls == [("snapshot", SESSION, work_dir, "Stop")]  # ...but the code is saved
    assert "no transcript yet" in outcome


def test_session_start_takes_the_baseline_and_backs_up_branches(
    client: RecordingClient, snapshots: RecordingSnapshots, work_dir: Path, tmp_path: Path
) -> None:
    outcome = _handler(client, snapshots, tmp_path).handle(
        _payload("SessionStart", tmp_path / "later.jsonl", work_dir, source="startup")
    )
    assert snapshots.calls == [("start", SESSION, work_dir, None)]
    assert "code: snapshot 0001, pushed" in outcome


@pytest.mark.parametrize("event", ["PreCompact"])
def test_events_that_hold_claude_up_or_repeat_do_not_snapshot(
    client: RecordingClient,
    snapshots: RecordingSnapshots,
    transcript: Path,
    work_dir: Path,
    tmp_path: Path,
    event: str,
) -> None:
    _handler(client, snapshots, tmp_path).handle(_payload(event, transcript, work_dir))
    assert snapshots.calls == []  # the snapshot after the response (Stop) covers these


def test_a_sub_agent_stop_does_not_snapshot(
    client: RecordingClient,
    snapshots: RecordingSnapshots,
    transcript: Path,
    work_dir: Path,
    tmp_path: Path,
) -> None:
    _handler(client, snapshots, tmp_path).handle(
        _payload("SubagentStop", transcript, work_dir, agent_type="Explore")
    )
    assert snapshots.calls == []


def test_without_a_working_directory_nothing_is_snapshotted(
    client: RecordingClient, snapshots: RecordingSnapshots, transcript: Path, tmp_path: Path
) -> None:
    handler = _handler(client, snapshots, tmp_path)
    handler.handle(_payload("Stop", transcript, None))
    handler.handle(_payload("SessionStart", transcript, None, source="startup"))
    assert snapshots.calls == []


def test_a_disabled_snapshot_service_adds_nothing_to_the_log_line(
    client: RecordingClient, transcript: Path, work_dir: Path, tmp_path: Path
) -> None:
    disabled = RecordingSnapshots(note=None)  # the real service returns None when switched off
    outcome = _handler(client, disabled, tmp_path).handle(_payload("Stop", transcript, work_dir))
    assert "code:" not in outcome


def test_a_handler_without_a_snapshot_service_works_as_before(
    client: RecordingClient, transcript: Path, work_dir: Path, tmp_path: Path
) -> None:
    outcome = _handler(client, None, tmp_path).handle(_payload("Stop", transcript, work_dir))
    assert "code:" not in outcome and client.synced


def test_session_end_passes_the_working_directory_to_the_background_flush(
    client: RecordingClient,
    snapshots: RecordingSnapshots,
    transcript: Path,
    work_dir: Path,
    tmp_path: Path,
) -> None:
    flushed: list[tuple[str, Path | None, Path | None]] = []
    outcome = _handler(client, snapshots, tmp_path, flushed).handle(
        _payload("SessionEnd", transcript, work_dir)
    )
    assert flushed == [(SESSION, transcript, work_dir)]  # the flush takes the final snapshot
    assert snapshots.calls == []  # nothing is done inline: SessionEnd has almost no time
    assert outcome == "end queued; flush started in the background"


def test_a_prompt_snapshots_what_a_person_changed_since_the_last_one(
    client: RecordingClient,
    snapshots: RecordingSnapshots,
    transcript: Path,
    work_dir: Path,
    tmp_path: Path,
) -> None:
    _handler(client, snapshots, tmp_path).handle(_payload("UserPromptSubmit", transcript, work_dir))
    assert snapshots.calls == [("snapshot", SESSION, work_dir, "UserPromptSubmit")]
    assert snapshots.contexts[0] is not None and snapshots.contexts[0].actor == "human"


@pytest.mark.parametrize(
    ("tool", "actor"),
    [("Edit", "ai_edit"), ("Write", "ai_edit"), ("Bash", "ai_command"), ("Other", "ai_other")],
)
def test_after_a_tool_call_the_snapshot_says_who_made_the_change(
    client: RecordingClient,
    snapshots: RecordingSnapshots,
    transcript: Path,
    work_dir: Path,
    tmp_path: Path,
    tool: str,
    actor: str,
) -> None:
    transcript.write_text(
        json.dumps({"type": "user", "uuid": "p1", "message": {"content": "add a parser please"}})
        + "\n"
        + json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "content": []}})
        + "\n",
        encoding="utf-8",
    )
    text = json.dumps(
        {"hook_event_name": "PostToolUse", "session_id": SESSION, "cwd": str(work_dir),
         "transcript_path": str(transcript), "tool_name": tool, "tool_use_id": "toolu_1",
         "tool_input": {"file_path": "src/a.py"}}
    )  # fmt: skip
    payload = parse_payload(text)
    assert payload is not None
    outcome = _handler(client, snapshots, tmp_path).handle(payload)

    context = snapshots.contexts[0]
    assert snapshots.calls == [("snapshot", SESSION, work_dir, "PostToolUse")]
    assert context is not None and context.actor == actor and context.tool == tool
    assert context.tool_use_id == "toolu_1" and context.model == "claude-opus-5-5"
    assert context.prompt_id == "p1" and context.prompt_excerpt == "add a parser please"
    assert context.target == "src/a.py" and "code: snapshot" in outcome
    assert client.synced == []  # no upload here: a tool call must stay fast
