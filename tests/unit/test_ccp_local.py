"""End-to-end test of the local capture: real hook process, real Git, fake transcript."""

import json
import subprocess
import sys
from pathlib import Path

import ccp_local
from ccp_local import cli, store

PACKAGE = Path(ccp_local.__file__).parent
SID = "11112222-3333-4444-5555-666677778888"


def _hook(env: dict, payload: dict) -> None:
    out = subprocess.run([sys.executable, str(PACKAGE), "hook"], input=json.dumps(payload),
                         env=env, capture_output=True, text=True, check=True)
    assert out.stdout == ""  # stdout of UserPromptSubmit would go into Claude's context


def _line(kind: str, ts: str, content: object, **extra: object) -> str:
    entry = {"type": kind, "timestamp": ts, "sessionId": SID, "cwd": "x", "uuid": ts,
             "message": {"role": kind, "content": content, **extra}}
    return json.dumps(entry)


def test_session_is_captured_turn_by_turn(tmp_path, monkeypatch):
    home, repo = tmp_path / "captures", tmp_path / "proj"
    repo.mkdir()
    (repo / "a.txt").write_text("one\n")
    monkeypatch.setenv("CCP_LOCAL_HOME", str(home))
    env = {**__import__("os").environ, "CCP_LOCAL_HOME": str(home)}
    transcript = tmp_path / f"{SID}.jsonl"
    base = {"session_id": SID, "cwd": str(repo), "transcript_path": str(transcript)}

    _hook(env, {**base, "hook_event_name": "SessionStart"})
    (repo / "a.txt").write_text("one\nhuman edit\n")
    _hook(env, {**base, "hook_event_name": "UserPromptSubmit", "prompt": "add b"})
    (repo / "b.txt").write_text("by claude\n")
    _hook(env, {**base, "hook_event_name": "PostToolUse", "tool_name": "Write",
                "tool_use_id": "toolu_1"})
    _hook(env, {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_use_id": "toolu_2"})  # nothing changed -> no snapshot
    usage = {"input_tokens": 10, "output_tokens": 5}
    transcript.write_text("\n".join([
        _line("user", store.now(), "add b"),
        _line("assistant", store.now(), [{"type": "thinking", "thinking": "plan"},
                                         {"type": "tool_use", "id": "toolu_1", "name": "Write",
                                          "input": {"file_path": "b.txt"}}],
              id="m1", model="claude-opus-5-5", usage=usage),
        _line("assistant", store.now(), [{"type": "text", "text": "x"}],  # same API message
              id="m1", model="claude-opus-5-5", usage=usage),
        _line("user", store.now(), [{"type": "tool_result", "tool_use_id": "toolu_1",
                                     "content": "ok"}]),
        _line("assistant", store.now(), [{"type": "text", "text": "Done."}],
              id="m2", model="claude-opus-5-5", usage=usage),
    ]) + "\n")
    _hook(env, {**base, "hook_event_name": "Stop"})

    pdir = next(home.glob("proj__*"))
    sdir = next((pdir / "sessions").glob(f"*__{SID[:8]}"))
    assert (sdir / "raw" / "transcript.jsonl").read_bytes() == transcript.read_bytes()
    cap = json.loads((sdir / "capture.json").read_text())
    turn = cap["turns"][0]
    assert cap["turn_count"] == 1 and turn["prompt"] == "add b"
    assert turn["tokens"]["output_tokens"] == 10  # m1 counted once
    assert [s["kind"] for s in turn["steps"]] == ["thinking", "tool", "text", "text"]
    assert turn["steps"][1]["result"] == "ok" and turn["steps"][1]["snapshot"]
    changes = {c["label"]: (c["by"], c["files"]) for c in turn["changes"]}
    assert changes["before prompt"] == ("human", ["M a.txt"])
    assert changes["Write"] == ("claude", ["A b.txt"])
    assert "Bash" not in changes
    assert "## Turn 1" in (sdir / "report.md").read_text()
    assert "<h2>Turn 1" in (sdir / "report.html").read_text()
    log = subprocess.run(["git", "--git-dir", str(pdir / "shadow.git"), "log", "--format=%an %s"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    assert log[:3] == ["claude [claude] Write", "human [human] before prompt",
                       "human [human] before session"]
    assert not (home / "errors.log").exists()
    assert not (repo / ".git").exists()  # the project itself is never touched


def test_install_replaces_old_hooks_and_keeps_others(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "python -m ccp.capture hook"},
                            {"type": "command", "command": "notify-send done"}]}]}}))
    cli.main(["install", "--settings", str(settings)])
    data = json.loads(settings.read_text())
    commands = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert commands[0] == "notify-send done" and "ccp_local" in commands[1]
    assert len(commands) == 2 and data["model"] == "opus"
    assert data["hooks"]["PostToolUse"][0]["matcher"].startswith("Edit|Write")
    cli.main(["uninstall", "--settings", str(settings)])
    data = json.loads(settings.read_text())
    assert list(data["hooks"]) == ["Stop"] and len(data["hooks"]["Stop"][0]["hooks"]) == 1
