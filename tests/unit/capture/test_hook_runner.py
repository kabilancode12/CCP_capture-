"""run_hook: whatever happens, it returns 0, prints nothing and logs no content."""

import json
from pathlib import Path

import pytest

from ccp.capture.hook_runner import LOG_FILE_NAME, run_hook

TOKEN = "hook" + "-runner-test-token"  # invented; built from pieces for the secret scanner


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "CCP_INGEST_TOKEN": TOKEN,
        "CCP_API_URL": "http://127.0.0.1:1",  # nothing listens here: the API is "down"
        "CCP_STATE_DIR": str(tmp_path / "state"),
        "CCP_CLIENT_TIMEOUT_SECONDS": "1",
        **extra,
    }


def _log(tmp_path: Path) -> str:
    return (tmp_path / "state" / LOG_FILE_NAME).read_text(encoding="utf-8")


def _payload(tmp_path: Path, event: str = "Stop", **extra: str) -> str:
    return json.dumps(
        {
            "hook_event_name": event,
            "session_id": "sess-1",
            "transcript_path": str(tmp_path / "sess-1.jsonl"),
            **extra,
        }
    )


@pytest.mark.parametrize("stdin", ["", "garbage", "[1,2]", "{}", '{"hook_event_name":"Stop"}'])
def test_unusable_input_returns_zero_and_prints_nothing(
    stdin: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_hook(stdin, _env(tmp_path), tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert "unusable hook payload" in _log(tmp_path)


def test_missing_settings_return_zero_and_are_logged_without_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_hook(_payload(tmp_path), {"CCP_STATE_DIR": str(tmp_path / "state")}, tmp_path) == 0
    assert capsys.readouterr().out == ""
    assert "settings invalid: Invalid settings: - CCP_INGEST_TOKEN is required" in _log(tmp_path)


def test_missing_settings_are_logged_in_the_default_folder_when_no_state_dir_is_set(
    tmp_path: Path,
) -> None:
    assert run_hook(_payload(tmp_path), {}, tmp_path) == 0
    log = (tmp_path / ".ccp" / "capture" / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert "CCP_INGEST_TOKEN is required" in log


def test_an_unreachable_api_returns_zero_and_the_hook_is_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript = tmp_path / "sess-1.jsonl"
    transcript.write_text('{"type":"user","message":"a private prompt"}\n', encoding="utf-8")

    assert run_hook(_payload(tmp_path), _env(tmp_path), tmp_path) == 0

    assert capsys.readouterr().out == ""  # SessionStart output would reach the conversation
    log = _log(tmp_path)
    assert "Stop session=sess-1" in log and "unavailable" in log
    assert "a private prompt" not in log  # no content
    assert TOKEN not in log  # no token


def test_session_start_prints_nothing_even_though_claude_code_reads_its_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run_hook(_payload(tmp_path, "SessionStart", source="startup"), _env(tmp_path), tmp_path)
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert "SessionStart session=sess-1" in _log(tmp_path)


def test_an_unexpected_error_is_logged_and_still_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ccp.capture import hook_runner

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(hook_runner, "build_handler", explode)
    assert run_hook(_payload(tmp_path), _env(tmp_path), tmp_path) == 0
    assert capsys.readouterr().out == ""
    assert "hook failed" in _log(tmp_path) and "simulated bug" in _log(tmp_path)


def test_an_unwritable_log_folder_does_not_stop_the_hook(tmp_path: Path) -> None:
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x", encoding="utf-8")
    env = _env(tmp_path, CCP_STATE_DIR=str(blocker / "state"))  # a folder cannot be created there
    assert run_hook(_payload(tmp_path), env, tmp_path) == 0


def test_repeated_hooks_do_not_pile_up_log_handlers(tmp_path: Path) -> None:
    import logging

    before = len(logging.getLogger("ccp.capture").handlers)
    for _ in range(3):
        run_hook(_payload(tmp_path), _env(tmp_path), tmp_path)
    assert len(logging.getLogger("ccp.capture").handlers) == before
    stop_lines = [line for line in _log(tmp_path).splitlines() if "Stop session=sess-1" in line]
    assert len(stop_lines) == 3  # one line per hook run: not two (a duplicated handler) and not six
