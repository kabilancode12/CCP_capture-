"""The hooks, run exactly as Claude Code runs them: a new process per event, JSON on stdin.

A scripted session drives ``python -m ccp.capture hook`` against the real API, PostgreSQL and
MinIO, and checks what ends up stored. The account and machine facts come from a made-up home
folder, never from the real one.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ccp.capture.hook_runner import LOG_FILE_NAME
from ccp.db.connection import connect
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore

from .conftest import API_TOKEN, RunningApi

pytestmark = pytest.mark.integration

SESSION = "sess-hooks-1"


class Machine:
    """A pretend developer machine: a home folder, a state folder, and a transcript location."""

    def __init__(self, root: Path, api_url: str) -> None:
        self.home = root / "home"
        self.state = root / "state"
        self.transcript = root / "projects" / f"{SESSION}.jsonl"
        self.home.mkdir()
        account = {
            "displayName": "Dev",
            "emailAddress": "dev@example.com",
            "organizationName": "Acme",
        }
        (self.home / ".claude.json").write_text(
            json.dumps({"oauthAccount": account}), encoding="utf-8"
        )
        self.api_url = api_url

    def env(self) -> dict[str, str]:
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("CCP_", "CLAUDE_CODE_"))}
        return {
            **clean,
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),  # Windows
            "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CCP_API_URL": self.api_url,
            "CCP_INGEST_TOKEN": API_TOKEN,
            "CCP_STATE_DIR": str(self.state),
            "CCP_CLIENT_TIMEOUT_SECONDS": "5",
        }

    def hook(
        self, event: str, session: str = SESSION, **extra: str
    ) -> subprocess.CompletedProcess[bytes]:
        """Run one hook as Claude Code would."""
        payload = {
            "hook_event_name": event,
            "session_id": session,
            "transcript_path": str(
                self.transcript if session == SESSION else self.other_transcript
            ),
            **extra,
        }
        return subprocess.run(  # noqa: S603  (fixed arguments)
            [sys.executable, "-m", "ccp.capture", "hook"],
            input=json.dumps(payload).encode(),
            env=self.env(),
            capture_output=True,
            timeout=90,
            check=False,
        )

    other_transcript = Path("unused")

    def log(self) -> str:
        return (self.state / LOG_FILE_NAME).read_text(encoding="utf-8")


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write(path: Path, *entries: object, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(_compact(entry) + "\n" for entry in entries))


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _stored(store: ObjectStore, session: str = SESSION) -> str:
    return "".join(store.get(key).decode() for key in store.list_keys(f"sessions/{session}/"))


def _wait_until(condition: Any, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def machine(tmp_path: Path, api: RunningApi) -> Machine:
    return Machine(tmp_path, api.url)


def _assert_quiet_success(result: subprocess.CompletedProcess[bytes]) -> None:
    """A hook must exit 0 and print nothing (SessionStart output reaches the conversation)."""
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == b"" and result.stderr == b""


# ---------------------------------------------------------------- a scripted session


def test_a_whole_session_is_captured_through_the_hooks(
    machine: Machine,
    database: DatabaseSettings,
    object_store: ObjectStore,
    fakes: dict[str, str],
) -> None:
    # 1. SessionStart: the transcript file does not exist yet (as on Claude Code 2.1.289).
    _assert_quiet_success(machine.hook("SessionStart", source="startup"))
    env = _rows(
        database, "SELECT entrypoint, surface, account_email, claude_code_version FROM session_env"
    )
    assert env == [("cli", "terminal", "dev@example.com", None)]  # version not known yet
    assert object_store.list_keys("sessions/") == []  # nothing to upload yet

    # 2. The transcript appears; the first prompt is submitted.
    _write(
        machine.transcript,
        {"type": "user", "version": "2.1.289", "message": f"my mail is {fakes['email']}"},
        {
            "type": "attachment",
            "attachment": {"type": "environment", "snapshot": {"shell": "PowerShell (primary)"}},
        },
    )
    _assert_quiet_success(machine.hook("UserPromptSubmit"))
    assert fakes["email"] not in _stored(object_store)  # redacted before it left the machine
    assert "[REDACTED:email]" in _stored(object_store)
    done = _rows(database, "SELECT claude_code_version, tool_shell FROM session_env")
    assert done == [("2.1.289", "PowerShell (primary)")]  # the second stage completed the record

    # 3. Claude answers; Stop uploads the new lines as a second chunk.
    _write(machine.transcript, {"type": "assistant", "uuid": "a1"}, append=True)
    _assert_quiet_success(machine.hook("Stop"))
    assert len(object_store.list_keys(f"sessions/{SESSION}/main/")) == 2

    # 4. Claude's internal helper "stops": skipped. A real sub-agent stops: its files are uploaded.
    agents = machine.transcript.with_suffix("") / "subagents"
    agents.mkdir(parents=True)
    _write(agents / "agent-e1.jsonl", {"type": "user", "uuid": "g1", "isSidechain": True})
    (agents / "agent-e1.meta.json").write_text('{"agentType":"Explore"}', encoding="utf-8")
    _assert_quiet_success(machine.hook("SubagentStop", agent_type=""))
    assert not any("agent-e1" in k for k in object_store.list_keys(f"sessions/{SESSION}/"))
    _assert_quiet_success(machine.hook("SubagentStop", agent_type="Explore"))
    assert any("agent-e1/" in k for k in object_store.list_keys(f"sessions/{SESSION}/"))
    assert any("agent-e1.meta/" in k for k in object_store.list_keys(f"sessions/{SESSION}/"))

    # 5. /compact: PreCompact syncs; SessionStart runs again for the same session. Nothing breaks
    #    and nothing is stored twice.
    keys_before = object_store.list_keys("")
    _assert_quiet_success(machine.hook("PreCompact", trigger="manual"))
    _assert_quiet_success(machine.hook("SessionStart", source="compact"))
    assert object_store.list_keys("") == keys_before

    # 6. More output, then SessionEnd: the hook returns at once; a background flush uploads it.
    _write(machine.transcript, {"type": "assistant", "uuid": "a2"}, append=True)
    started = time.monotonic()
    _assert_quiet_success(machine.hook("SessionEnd", reason="prompt_input_exit"))
    assert time.monotonic() - started < 15  # it did not wait for the upload
    assert _wait_until(
        lambda: '"uuid":"a2"' in _stored(object_store)
    ), "the background flush did not run"

    # The log tells the story without any content or token.
    log = machine.log()
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SubagentStop",
        "PreCompact",
        "SessionEnd",
    ):
        assert f"{event} session={SESSION}" in log
    assert "skipped: internal helper" in log and "flush started in the background" in log
    assert fakes["email"] not in log and API_TOKEN not in log and "my mail is" not in log

    # Everything arrived once: one session row, one open job queued, the redaction recorded.
    assert _rows(database, "SELECT count(*) FROM session") == [(1,)]
    assert _rows(database, "SELECT DISTINCT rule_id FROM redaction_log") == [("email",)]


def test_hooks_survive_the_api_being_down_and_catch_up_afterwards(
    machine: Machine, api: RunningApi, object_store: ObjectStore
) -> None:
    _write(machine.transcript, {"type": "user", "uuid": "u1"})
    _assert_quiet_success(machine.hook("Stop"))
    assert '"uuid":"u1"' in _stored(object_store)

    api.stop()  # the API goes away mid-session
    _write(machine.transcript, {"type": "user", "uuid": "u2"}, append=True)
    _assert_quiet_success(machine.hook("Stop"))  # still exit 0 and silent
    assert "unavailable" in machine.log()

    api.start()  # and comes back
    _write(machine.transcript, {"type": "user", "uuid": "u3"}, append=True)
    _assert_quiet_success(machine.hook("Stop"))
    stored = _stored(object_store)
    assert all(f'"uuid":"u{n}"' in stored for n in (1, 2, 3))  # nothing was lost
    assert stored.count('"uuid":"u2"') == 1  # and nothing was stored twice


def test_a_new_session_starting_uploads_what_an_earlier_one_left_behind(
    machine: Machine, api: RunningApi, tmp_path: Path, object_store: ObjectStore
) -> None:
    api.stop()
    _write(machine.transcript, {"type": "user", "uuid": "left-behind"})
    _assert_quiet_success(machine.hook("Stop"))  # fails: the API is down; the position is kept
    assert object_store.list_keys("sessions/") == []

    api.start()
    new_session = "sess-hooks-2"
    machine.other_transcript = tmp_path / "projects" / f"{new_session}.jsonl"
    _assert_quiet_success(machine.hook("SessionStart", session=new_session, source="startup"))

    assert '"uuid":"left-behind"' in _stored(object_store, SESSION)  # caught up at start-up
    assert "caught up 1 earlier session(s)" in machine.log()
