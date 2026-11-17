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


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " True "])
def test_the_kill_switch_makes_the_hook_do_nothing_at_all(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(tmp_path, CCP_CAPTURE_DISABLED=value)
    assert run_hook(_payload(tmp_path, "SessionStart", source="startup"), env, tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert not (tmp_path / "state").exists()  # no log, no state, no upload


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_other_values_of_the_kill_switch_leave_capture_on(value: str, tmp_path: Path) -> None:
    env = _env(tmp_path, CCP_CAPTURE_DISABLED=value)
    assert run_hook(_payload(tmp_path), env, tmp_path) == 0
    assert "Stop session=sess-1" in _log(tmp_path)


def test_session_end_queues_the_end_even_without_a_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []
    monkeypatch.setattr("ccp.capture.hook_runner.spawn_detached", started.append)
    payload = _payload(tmp_path, "SessionEnd", reason="prompt_input_exit", cwd=str(tmp_path))

    assert run_hook(payload, _env(tmp_path), tmp_path) == 0

    queued = list((tmp_path / "state" / "sess-1").glob("lifecycle-end-*.marker"))
    assert len(queued) == 1
    assert json.loads(queued[0].read_text(encoding="utf-8"))["reason"] == "prompt_input_exit"
    (command,) = started
    assert command[-4:] == ["--session-id", "sess-1", "--cwd", str(tmp_path)]  # no transcript


def test_an_unreachable_api_costs_session_start_one_request_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Off VPN, packets are dropped and every request waits for the full timeout; SessionStart has
    # 30 seconds. Once one request could not reach the API, no other is tried in that hook run.
    import urllib.error
    import urllib.request

    monkeypatch.setattr("ccp.capture.hook_runner.spawn_detached", lambda command: None)
    older = tmp_path / "older.jsonl"
    older.write_text('{"type":"user"}\n', encoding="utf-8")
    for event in ("Stop", "SessionEnd"):  # an earlier chat with a transcript and a waiting end
        stdin = json.dumps(
            {"hook_event_name": event, "session_id": "older", "transcript_path": str(older)}
        )
        run_hook(stdin, _env(tmp_path), tmp_path)
    (tmp_path / "sess-1.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")  # resumed

    attempts: list[str] = []

    def unreachable(self: object, request: urllib.request.Request, **kwargs: object) -> None:
        attempts.append(request.get_method())
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", unreachable)
    payload = _payload(tmp_path, "SessionStart", source="resume")

    assert run_hook(payload, _env(tmp_path), tmp_path) == 0

    assert len(attempts) == 1
    assert "SessionStart session=sess-1" in _log(tmp_path)
