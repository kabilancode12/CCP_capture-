import json
import sys
from pathlib import Path

import pytest

from ccp.capture.cli import main
from ccp.capture.hook_settings import TOKEN_PLACEHOLDER, build_hook_settings, hook_command
from ccp.capture.hooks import HOOK_TIMEOUTS

EXPECTED_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "SessionEnd",
}


def test_the_command_uses_this_python_with_forward_slashes() -> None:
    command = hook_command()
    assert command == f'"{Path(sys.executable).as_posix()}" -m ccp.capture hook'
    assert "\\" not in command  # Git Bash on Windows would eat backslashes


def test_every_event_is_registered_with_its_time_limit() -> None:
    settings = build_hook_settings("run-me", "http://api.example:8000")
    assert set(settings["hooks"]) == EXPECTED_EVENTS
    for event, entries in settings["hooks"].items():
        (hook,) = entries[0]["hooks"]
        assert hook == {"type": "command", "command": "run-me", "timeout": HOOK_TIMEOUTS[event]}


def test_session_end_has_a_short_time_limit_because_it_only_starts_a_background_flush() -> None:
    assert HOOK_TIMEOUTS["SessionEnd"] <= 5


def test_the_env_block_has_the_api_address_and_only_a_token_placeholder() -> None:
    env = build_hook_settings("run-me", "http://api.example:8000")["env"]
    assert env == {"CCP_API_URL": "http://api.example:8000", "CCP_INGEST_TOKEN": TOKEN_PLACEHOLDER}


def test_a_real_token_in_the_environment_is_never_copied_into_the_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = "a-real-looking-" + "token-value-1234"
    monkeypatch.setenv("CCP_INGEST_TOKEN", real)
    assert main(["hook-settings"]) == 0
    assert real not in capsys.readouterr().out


def test_the_command_prints_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["hook-settings", "--api-url", "https://ccp.example.com"]) == 0
    settings = json.loads(capsys.readouterr().out)
    assert settings["env"]["CCP_API_URL"] == "https://ccp.example.com"
    assert set(settings["hooks"]) == EXPECTED_EVENTS
