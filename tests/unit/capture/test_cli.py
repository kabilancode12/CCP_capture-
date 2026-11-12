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
