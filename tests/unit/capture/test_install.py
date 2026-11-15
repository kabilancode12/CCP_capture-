"""Tests for ``ccp-capture install``, ``uninstall`` and ``status``. Never touch the real settings."""

import json
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from ccp.capture import cli
from ccp.capture.hooks import HOOK_TIMEOUTS
from ccp.capture.install import (
    InstallError,
    collect_status,
    install,
    installed_events,
    pending_uploads,
    uninstall,
)
from ccp.capture.state import StateStore

TOKEN = "t" * 24
URL = "http://127.0.0.1:8000"
COMMAND = '"/usr/bin/python3" -m ccp.capture hook'


def read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".bak-*"))


@pytest.fixture
def settings(tmp_path: Path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def test_install_creates_the_file_with_env_and_six_hooks(settings: Path) -> None:
    result = install(settings, URL, TOKEN, command=COMMAND)
    data = read(settings)
    assert result.changed and result.backup is None
    assert data["env"] == {
        "CCP_API_URL": URL,
        "CCP_INGEST_TOKEN": TOKEN,
        "CCP_SNAPSHOTS": "true",
    }
    assert set(data["hooks"]) == set(HOOK_TIMEOUTS)
    for event, timeout in HOOK_TIMEOUTS.items():
        assert data["hooks"][event] == [
            {"hooks": [{"type": "command", "command": COMMAND, "timeout": timeout}]}
        ]


def test_the_command_defaults_to_the_current_python(settings: Path) -> None:
    import sys

    install(settings, URL, TOKEN)
    command = read(settings)["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert Path(sys.executable).as_posix() in command
    assert command.endswith("-m ccp.capture hook")


def test_install_keeps_every_other_setting_and_other_hooks(settings: Path) -> None:
    settings.parent.mkdir(parents=True)
    other = {"type": "command", "command": "other-tool notify", "timeout": 3}
    settings.write_text(
        json.dumps(
            {
                "model": "x",
                "theme": "dark",
                "env": {"MINE": "1"},
                "hooks": {
                    "Stop": [{"matcher": "*", "hooks": [other]}],
                    "PostToolUse": [{"hooks": [other]}],
                },
            }
        ),
        encoding="utf-8",
    )
    install(settings, URL, TOKEN, command=COMMAND)
    data = read(settings)
    assert data["model"] == "x" and data["theme"] == "dark"
    assert data["env"]["MINE"] == "1" and data["env"]["CCP_INGEST_TOKEN"] == TOKEN
    assert data["hooks"]["PostToolUse"] == [{"hooks": [other]}]
    stop = data["hooks"]["Stop"]
    assert stop[0] == {"matcher": "*", "hooks": [other]}
    assert stop[1]["hooks"][0]["command"] == COMMAND


def test_a_backup_is_made_before_changing_an_existing_file(settings: Path) -> None:
    settings.parent.mkdir(parents=True)
    original = '{"theme": "dark"}'
    settings.write_text(original, encoding="utf-8")
    result = install(settings, URL, TOKEN, command=COMMAND)
    assert result.backup is not None and result.backup in backups(settings)
    assert result.backup.read_text(encoding="utf-8") == original


def test_install_twice_is_idempotent_and_makes_no_duplicates(settings: Path) -> None:
    install(settings, URL, TOKEN, command=COMMAND)
    first = settings.read_text(encoding="utf-8")
    again = install(settings, URL, TOKEN, command=COMMAND)
    assert not again.changed and again.backup is None
    assert settings.read_text(encoding="utf-8") == first
    assert backups(settings) == []


def test_install_replaces_our_hook_with_a_new_python_path(settings: Path) -> None:
    install(settings, URL, TOKEN, command='"/old/python" -m ccp.capture hook')
    install(settings, URL, TOKEN, command=COMMAND)
    for groups in read(settings)["hooks"].values():
        commands = [h["command"] for g in groups for h in g["hooks"]]
        assert commands == [COMMAND]


def test_install_updates_the_url_and_token(settings: Path) -> None:
    install(settings, URL, TOKEN, command=COMMAND)
    install(settings, "https://ccp.example.com/", "n" * 20, command=COMMAND)
    env = read(settings)["env"]
    assert env["CCP_API_URL"] == "https://ccp.example.com"
    assert env["CCP_INGEST_TOKEN"] == "n" * 20


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"'])
def test_a_file_that_is_not_a_json_object_is_refused_and_left_alone(
    settings: Path, content: str
) -> None:
    settings.parent.mkdir(parents=True)
    settings.write_text(content, encoding="utf-8")
    with pytest.raises(InstallError):
        install(settings, URL, TOKEN, command=COMMAND)
    assert settings.read_text(encoding="utf-8") == content
    assert backups(settings) == []


def test_hooks_that_are_not_an_object_are_refused(settings: Path) -> None:
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": []}', encoding="utf-8")
    with pytest.raises(InstallError, match="hooks"):
        install(settings, URL, TOKEN, command=COMMAND)


@pytest.mark.parametrize(
    ("url", "token"), [("ftp://x", TOKEN), ("", TOKEN), (URL, "short"), (URL, "")]
)
def test_bad_values_are_refused(settings: Path, url: str, token: str) -> None:
    with pytest.raises(InstallError):
        install(settings, url, token, command=COMMAND)
    assert not settings.exists()


@pytest.mark.skipif(os.name == "nt", reason="file modes are not used on Windows")
def test_the_file_with_the_token_is_readable_by_its_owner_only(settings: Path) -> None:
    install(settings, URL, TOKEN, command=COMMAND)
    assert settings.stat().st_mode & 0o077 == 0


def test_uninstall_removes_only_our_hooks_and_env_keys(settings: Path) -> None:
    settings.parent.mkdir(parents=True)
    other = {"type": "command", "command": "other-tool notify"}
    settings.write_text(
        json.dumps(
            {"theme": "dark", "env": {"MINE": "1"}, "hooks": {"Stop": [{"hooks": [other]}]}}
        ),
        encoding="utf-8",
    )
    install(settings, URL, TOKEN, command=COMMAND)
    result = uninstall(settings)
    assert result.changed and result.backup is not None
    assert read(settings) == {
        "theme": "dark",
        "env": {"MINE": "1"},
        "hooks": {"Stop": [{"hooks": [other]}]},
    }


def test_uninstall_leaves_no_empty_blocks(settings: Path) -> None:
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme": "dark"}', encoding="utf-8")
    install(settings, URL, TOKEN, command=COMMAND)
    uninstall(settings)
    assert read(settings) == {"theme": "dark"}


def test_uninstall_without_a_file_or_without_our_settings_changes_nothing(settings: Path) -> None:
    assert not uninstall(settings).changed
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme": "dark", "hooks": {"Stop": []}}', encoding="utf-8")
    assert not uninstall(settings).changed
    assert read(settings) == {"theme": "dark", "hooks": {"Stop": []}}


def test_uninstall_refuses_an_invalid_file(settings: Path) -> None:
    settings.parent.mkdir(parents=True)
    settings.write_text("{oops", encoding="utf-8")
    with pytest.raises(InstallError):
        uninstall(settings)


def test_installed_events_lists_events_with_our_hook(settings: Path) -> None:
    assert installed_events(settings) == []
    install(settings, URL, TOKEN, command=COMMAND)
    assert installed_events(settings) == list(HOOK_TIMEOUTS)


# ---------------------------------------------------------------- status


class _Api(BaseHTTPRequestHandler):
    """A tiny fake API: /health answers 200; the token probe accepts only ``TOKEN``."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        good = self.headers.get("Authorization") == f"Bearer {TOKEN}"
        self.send_response(400 if good else 401)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_api() -> Any:
    server = HTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_status_of_a_working_install(settings: Path, tmp_path: Path, fake_api: str) -> None:
    install(settings, fake_api, TOKEN, command=COMMAND)
    state_dir = tmp_path / "state"
    state = StateStore(state_dir)
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("12345678\n", encoding="utf-8")
    state.remember_transcript("chat1", transcript)
    state.set_offset("chat1", "main", 9)  # fully uploaded
    status = collect_status(settings, {"CCP_STATE_DIR": str(state_dir)})
    assert status.installed and status.missing_events == []
    assert status.reachable is True and status.token_valid is True
    assert status.pending_uploads == 0
    assert isinstance(status.last_upload, datetime)


def test_status_with_a_refused_token(settings: Path, tmp_path: Path, fake_api: str) -> None:
    install(settings, fake_api, "w" * 24, command=COMMAND)
    status = collect_status(settings, {"CCP_STATE_DIR": str(tmp_path / "s")})
    assert status.reachable is True and status.token_valid is False
    assert status.last_upload is None


def test_status_when_nothing_is_installed_and_the_api_is_down(
    settings: Path, tmp_path: Path
) -> None:
    env = {"CCP_API_URL": "http://127.0.0.1:1", "CCP_STATE_DIR": str(tmp_path / "s")}
    status = collect_status(settings, env)
    assert not status.installed and len(status.missing_events) == 6
    assert status.reachable is False and status.token_valid is None


def test_the_shell_overrides_the_installed_values(
    settings: Path, tmp_path: Path, fake_api: str
) -> None:
    install(settings, "http://127.0.0.1:1", TOKEN, command=COMMAND)
    status = collect_status(
        settings, {"CCP_API_URL": fake_api, "CCP_STATE_DIR": str(tmp_path / "s")}
    )
    assert status.reachable is True and status.token_valid is True


def test_pending_uploads_counts_waiting_events_and_unsent_bytes(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state")
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("abc\ndef\n", encoding="utf-8")
    state.remember_transcript("chat1", transcript)
    state.set_offset("chat1", "main", 4)  # half uploaded
    state.set_marker("chat2", "lifecycle-start-run-key-1", "{}")
    assert pending_uploads(tmp_path / "state") == 2
    assert pending_uploads(tmp_path / "nothing") == 0


# ---------------------------------------------------------------- the command line


def test_cli_install_status_uninstall(
    settings: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CCP_INGEST_TOKEN", raising=False)
    path = ["--settings-path", str(settings)]
    assert cli.main(["install", "--api-url", URL, "--token", TOKEN, *path]) == 0
    out = capsys.readouterr().out
    assert "installed" in out and TOKEN not in out
    assert cli.main(["install", "--api-url", URL, "--token", TOKEN, *path]) == 0
    assert "already installed" in capsys.readouterr().out
    monkeypatch.setenv("CCP_STATE_DIR", str(settings.parent / "state"))
    assert cli.main(["status", *path]) == 0
    status_out = capsys.readouterr().out
    assert "installed:       yes" in status_out and TOKEN not in status_out
    assert cli.main(["uninstall", *path]) == 0
    assert "removed" in capsys.readouterr().out
    assert installed_events(settings) == []


def test_cli_install_refuses_invalid_json_with_exit_code_1(
    settings: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.parent.mkdir(parents=True)
    settings.write_text("{oops", encoding="utf-8")
    code = cli.main(
        ["install", "--api-url", URL, "--token", TOKEN, "--settings-path", str(settings)]
    )
    assert code == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert settings.read_text(encoding="utf-8") == "{oops"


def test_cli_install_takes_the_token_from_the_environment(
    settings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CCP_INGEST_TOKEN", TOKEN)
    assert cli.main(["install", "--api-url", URL, "--settings-path", str(settings)]) == 0
    assert read(settings)["env"]["CCP_INGEST_TOKEN"] == TOKEN


def test_cli_install_without_any_token_fails(
    settings: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CCP_INGEST_TOKEN", raising=False)
    assert cli.main(["install", "--api-url", URL, "--settings-path", str(settings)]) == 1
    assert "token" in capsys.readouterr().err
