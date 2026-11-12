import io
import json
from pathlib import Path

import pytest

from ccp.verify import hook_probe
from ccp.verify.cli import main


def _log_lines(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_summary_hides_content_and_keeps_safe_values() -> None:
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "my password is hunter2",
        "tool_input": {"command": "echo secret"},
        "permission_mode": "default",
    }
    summary = hook_probe.summarise_payload(payload)
    assert summary["keys"] == ["hook_event_name", "permission_mode", "prompt", "tool_input"]
    assert summary["values"] == {
        "hook_event_name": "UserPromptSubmit",
        "permission_mode": "default",
    }
    assert "hunter2" not in json.dumps(summary)
    assert "echo secret" not in json.dumps(summary)


def test_path_values_are_reduced_to_whether_they_exist(tmp_path: Path) -> None:
    real = tmp_path / "transcript.jsonl"
    real.write_text("{}", encoding="utf-8")
    summary = hook_probe.summarise_payload(
        {"transcript_path": str(real), "agent_transcript_path": str(tmp_path / "missing.jsonl")}
    )
    assert summary["paths_exist"] == {"transcript_path": True, "agent_transcript_path": False}
    assert str(tmp_path) not in json.dumps(summary)


def test_run_appends_one_record_per_call(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "probe.jsonl"  # parent folder does not exist yet
    env = {hook_probe.LOG_PATH_VARIABLE: str(log), "CLAUDE_CODE_ENTRYPOINT": "cli"}

    hook_probe.run('{"hook_event_name": "SessionEnd", "reason": "other"}', env)
    hook_probe.run('{"hook_event_name": "Stop"}', env)

    records = _log_lines(log)
    assert [r["event"] for r in records] == ["SessionEnd", "Stop"]
    assert records[0]["entrypoint"] == "cli"
    assert records[0]["values"] == {"hook_event_name": "SessionEnd", "reason": "other"}


@pytest.mark.parametrize("bad_input", ["not json", "[1, 2]", ""])
def test_run_never_raises_and_logs_only_the_error_type(tmp_path: Path, bad_input: str) -> None:
    log = tmp_path / "probe.jsonl"
    hook_probe.run(bad_input, {hook_probe.LOG_PATH_VARIABLE: str(log)})
    last = _log_lines(log)[-1]
    # An empty payload is valid (treated as {}); the others are errors.
    assert last["event"] in {"<probe-error>", "<missing>"}
    assert "not json" not in json.dumps(last)


def test_run_stays_silent_when_the_log_is_unwritable(tmp_path: Path) -> None:
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x", encoding="utf-8")
    log = blocker / "probe.jsonl"  # its parent is a file, so it can never be created
    hook_probe.run("{}", {hook_probe.LOG_PATH_VARIABLE: str(log)})  # must not raise


def test_settings_register_every_event_with_the_command() -> None:
    snippet = hook_probe.settings_snippet('"py" -m ccp.verify hook-probe')
    assert set(snippet["hooks"]) == set(hook_probe.PROBE_EVENTS)
    assert "SessionEnd" in snippet["hooks"]
    hook = snippet["hooks"]["Stop"][0]["hooks"][0]
    assert hook["command"] == '"py" -m ccp.verify hook-probe'
    assert hook["type"] == "command"


def test_probe_command_uses_forward_slashes() -> None:
    assert "\\" not in hook_probe.probe_command()


def test_report_counts_events_and_lists_missing_ones() -> None:
    records = [
        {"at": "t1", "event": "SessionStart", "keys": ["a"], "values": {"source": "startup"}},
        {"at": "t2", "event": "Stop", "keys": ["b"], "values": {}},
        {"at": "t3", "event": "Stop", "keys": ["c"], "values": {}},
    ]
    report = hook_probe.render_report(records)
    assert "| Stop | 2 |" in report
    assert "source=startup" in report
    assert "Events that never fired:" in report
    assert "SessionEnd" in report.split("Events that never fired:")[1].splitlines()[0]


def test_cli_hook_probe_always_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(hook_probe.LOG_PATH_VARIABLE, str(tmp_path / "probe.jsonl"))
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert main(["hook-probe"]) == 0


def test_cli_probe_report_reads_the_log(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "probe.jsonl"
    hook_probe.append_record({"event": "Stop", "keys": [], "values": {}}, log)
    assert main(["probe-report", "--log", str(log)]) == 0
    assert "# Hook probe report" in capsys.readouterr().out


def test_cli_probe_report_without_log_returns_error(tmp_path: Path) -> None:
    assert main(["probe-report", "--log", str(tmp_path / "none.jsonl")]) == 2


def test_cli_probe_settings_prints_valid_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["probe-settings"]) == 0
    assert "SessionEnd" in json.loads(capsys.readouterr().out)["hooks"]
