"""The model providers. No real model is ever called: the command is a small fake script."""

import importlib.machinery
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ccp.decisions.provider import (
    AnthropicProvider,
    ClaudeCliProvider,
    ProviderError,
    choose_provider,
    parse_cli_output,
)
from ccp.decisions.settings import DecisionSettings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses a shell-script fake")


def _fake_command(folder: Path, body: str) -> str:
    """A fake `claude`: a Python script that records how it was called, then runs ``body``."""
    script = folder / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "prompt = sys.stdin.read()\n"
        "record = {'argv': sys.argv[1:], 'prompt': prompt, 'cwd': os.getcwd(),\n"
        "          'disabled': os.environ.get('CCP_CAPTURE_DISABLED')}\n"
        f"open({str(folder / 'call.json')!r}, 'w').write(json.dumps(record))\n"
        f"{body}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _call(folder: Path) -> dict[str, object]:
    return json.loads((folder / "call.json").read_text())  # type: ignore[no-any-return]


def test_the_command_gets_the_flags_the_prompt_on_stdin_and_capture_disabled(
    tmp_path: Path,
) -> None:
    command = _fake_command(
        tmp_path, "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'hello'}))"
    )
    provider = ClaudeCliProvider(timeout_seconds=30, command=command)
    assert provider.complete("be brief", "the question", "claude-opus-5-5") == "hello"
    call = _call(tmp_path)
    argv = call["argv"]
    assert isinstance(argv, list)
    assert argv[:5] == ["-p", "--output-format", "json", "--model", "claude-opus-5-5"]
    assert "be brief" in argv
    assert call["prompt"] == "the question"
    assert call["disabled"] == "1"
    assert call["cwd"] != os.getcwd()  # an empty temporary folder, not the project


def test_the_prompt_is_not_on_the_command_line(tmp_path: Path) -> None:
    command = _fake_command(tmp_path, "print(json.dumps({'subtype': 'success', 'result': 'x'}))")
    ClaudeCliProvider(30, command).complete("s", "SECRET QUESTION", "m")
    assert "SECRET QUESTION" not in " ".join(_call(tmp_path)["argv"])  # type: ignore[arg-type]


def test_a_missing_command_is_an_error() -> None:
    with pytest.raises(ProviderError, match="not found"):
        ClaudeCliProvider(30, "definitely-not-a-command-xyz").complete("s", "p", "m")


def test_a_command_that_takes_too_long_is_stopped(tmp_path: Path) -> None:
    command = _fake_command(tmp_path, "import time; time.sleep(30)")
    with pytest.raises(ProviderError, match="did not answer within 1 s"):
        ClaudeCliProvider(1, command).complete("s", "p", "m")


def test_a_failing_command_is_an_error_with_its_exit_code(tmp_path: Path) -> None:
    command = _fake_command(tmp_path, "sys.stderr.write('boom'); sys.exit(3)")
    with pytest.raises(ProviderError, match="code 3: boom"):
        ClaudeCliProvider(30, command).complete("s", "p", "m")


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("not json", "JSON"),
        ("[1, 2]", "JSON"),
        ('{"subtype": "success"}', "no text"),
        ('{"subtype": "success", "result": "  "}', "no text"),
        ('{"subtype": "error_max_turns", "result": "x"}', "error"),
        ('{"is_error": true, "result": "x"}', "error"),
    ],
)
def test_unusable_output_is_rejected(output: str, message: str) -> None:
    with pytest.raises(ProviderError, match=message):
        parse_cli_output(output)


def test_the_result_field_is_returned() -> None:
    assert (
        parse_cli_output('{"type":"result","subtype":"success","result":"{\\"a\\": 1}"}')
        == '{"a": 1}'
    )


def test_the_cli_is_used_without_an_api_key_or_without_the_package() -> None:
    settings = DecisionSettings()
    assert isinstance(choose_provider(settings, {}), ClaudeCliProvider)
    # anthropic is not installed in this project, so even a key falls back to the command
    try:
        import anthropic  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        assert isinstance(choose_provider(settings, {"ANTHROPIC_API_KEY": "x"}), ClaudeCliProvider)


def test_the_api_provider_asks_for_adaptive_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a fake ``anthropic`` module: the call has the right shape and no network is used."""
    seen: dict[str, object] = {}

    class FakeStream:
        def __enter__(self) -> "FakeStream":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def get_final_message(self) -> object:
            block = type("Block", (), {"type": "text", "text": "answer"})()
            return type("Message", (), {"stop_reason": "end_turn", "content": [block]})()

    class FakeMessages:
        def stream(self, **kwargs: object) -> FakeStream:
            seen.update(kwargs)
            return FakeStream()

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            self.messages = FakeMessages()

    module = type(sys)("anthropic")
    module.__spec__ = importlib.machinery.ModuleSpec("anthropic", None)
    module.Anthropic = FakeClient  # type: ignore[attr-defined]
    module.APIError = RuntimeError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)

    assert AnthropicProvider(30).complete("sys", "prompt", "claude-opus-5-5") == "answer"
    assert seen["model"] == "claude-opus-5-5"
    assert seen["thinking"] == {"type": "adaptive"}
    assert seen["system"] == "sys"
    assert isinstance(
        choose_provider(DecisionSettings(), {"ANTHROPIC_API_KEY": "k"}), AnthropicProvider
    )


def test_claude_is_found_in_the_users_own_bin_folder_when_the_path_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A background service has a short PATH; ~/.local/bin is searched as well."""
    from ccp.decisions.provider import find_command

    bin_folder = tmp_path / ".local" / "bin"
    bin_folder.mkdir(parents=True)
    tool = bin_folder / "claude"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("CCP_CLAUDE_COMMAND", raising=False)
    assert find_command("claude") == str(tool)
    assert find_command("no-such-command-ccp") is None
    monkeypatch.setenv("CCP_CLAUDE_COMMAND", str(tool))
    assert find_command("claude") == str(tool)


def test_the_tokens_cost_and_time_are_read_from_the_cli_output() -> None:
    from ccp.decisions.provider import parse_cli_usage

    output = json.dumps(
        {
            "result": "{}",
            "total_cost_usd": 0.0421,
            "duration_ms": 41234,
            "usage": {
                "input_tokens": 12,
                "output_tokens": 850,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 7000,
            },
        }
    )
    used = parse_cli_usage(output, "m")
    assert used is not None and used.as_dict() == {
        "model": "m",
        "input_tokens": 12,
        "output_tokens": 850,
        "cache_read_tokens": 100,
        "cache_creation_tokens": 7000,
        "cost_usd": 0.0421,
        "seconds": 41.2,
    }
    assert parse_cli_usage("not json", "m") is None
    assert parse_cli_usage(json.dumps({"result": "x"}), "m") is None


def test_the_command_leaves_out_connectors_skills_and_tools() -> None:
    """The call loads nothing but the instructions: no tools, connectors or skills."""
    arguments = ClaudeCliProvider(60, command=sys.executable).arguments("system text", "model-x")
    assert arguments[arguments.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in arguments
    assert arguments[arguments.index("--model") + 1] == "model-x"
    assert arguments[arguments.index("--system-prompt") + 1] == "system text"


def test_the_effort_level_is_passed_to_the_command_only_when_given() -> None:
    provider = ClaudeCliProvider(60, command=sys.executable)
    low = provider.arguments("s", "m", "low")
    assert low[low.index("--effort") + 1] == "low"
    assert "--effort" not in provider.arguments("s", "m")
