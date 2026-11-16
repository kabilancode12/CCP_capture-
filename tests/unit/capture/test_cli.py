from pathlib import Path

import pytest

from ccp.capture import cli
from ccp.capture.cli import main


def test_a_missing_token_is_reported_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("CCP_INGEST_TOKEN", raising=False)
    assert main(["sync", "--session-id", "s1", str(tmp_path / "t.jsonl")]) == 1
    assert "CCP_INGEST_TOKEN is required" in capsys.readouterr().err


def test_sync_needs_a_session_id() -> None:
    with pytest.raises(SystemExit):
        main(["sync", "t.jsonl"])


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_catch_up_with_nothing_known_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CCP_INGEST_TOKEN", "x" * 20)
    monkeypatch.setenv("CCP_STATE_DIR", str(tmp_path / "empty-state"))
    assert main(["catch-up"]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_summary_lists_errors_per_stream() -> None:
    from ccp.capture.client import StreamResult

    line, ok = cli._summary(
        "s1", [StreamResult("main", chunks=2, attachments=1), StreamResult("agent-a", error="boom")]
    )
    assert line == "s1: 2 chunk(s), 1 attachment(s) uploaded; agent-a: boom"
    assert not ok


class _FakeClient:
    """Stands in for the capture client in the flush command."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.calls: list[tuple[str, object]] = []
        _FakeClient.last = self

    last: "_FakeClient"

    def add_end_commit(self, session_id: str, commit: str | None) -> bool:
        self.calls.append(("add_end_commit", commit))
        return True

    def send_lifecycle(self, session_id: str) -> str | None:
        self.calls.append(("send_lifecycle", session_id))
        return "1 lifecycle event(s) sent"

    def sync_session(self, session_id: str, transcript: Path) -> list[object]:
        self.calls.append(("sync_session", transcript))
        return []


class _FakeSnapshots:
    def __init__(self) -> None:
        self.finished: list[Path] = []

    def finish(self, session_id: str, cwd: Path) -> str:
        self.finished.append(cwd)
        return "final snapshot taken"


@pytest.fixture
def snapshots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeSnapshots:
    fake = _FakeSnapshots()
    monkeypatch.setenv("CCP_INGEST_TOKEN", "x" * 20)
    monkeypatch.setenv("CCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "CaptureClient", _FakeClient)
    monkeypatch.setattr(cli, "snapshot_service_from", lambda settings, state, api=None: fake)
    monkeypatch.setattr(cli, "read_head", lambda folder: "ab" * 20)
    return fake


def test_the_flush_adds_the_commit_before_it_sends_the_end(
    snapshots: _FakeSnapshots, tmp_path: Path
) -> None:
    transcript = tmp_path / "s1.jsonl"
    assert main(["flush", "--session-id", "s1", "--cwd", str(tmp_path), str(transcript)]) == 0
    assert _FakeClient.last.calls == [
        ("add_end_commit", "ab" * 20),
        ("send_lifecycle", "s1"),
        ("sync_session", transcript),
    ]
    assert snapshots.finished == [tmp_path]


def test_the_flush_of_a_chat_without_a_transcript_takes_no_snapshot(
    snapshots: _FakeSnapshots, tmp_path: Path
) -> None:
    # Opened and closed without a message (most VS Code chats): no code changed.
    assert main(["flush", "--session-id", "s1", "--cwd", str(tmp_path)]) == 0
    assert [name for name, _ in _FakeClient.last.calls] == ["add_end_commit", "send_lifecycle"]
    assert snapshots.finished == []
