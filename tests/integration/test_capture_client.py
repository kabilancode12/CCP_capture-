"""The capture client against the real API, PostgreSQL and MinIO (see conftest.py).

These tests prove the promises of the client end to end: what is stored is redacted, attachments
are stored once, nothing is lost when the API goes away, and nothing is stored twice when it comes
back.
"""

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette

from ccp.capture.api import HttpApiClient
from ccp.capture.cli import main as capture_main
from ccp.capture.client import CaptureClient
from ccp.capture.environment import (
    HostInfo,
    collect_process_environment,
    collect_transcript_environment,
)
from ccp.capture.state import StateStore
from ccp.db.connection import connect
from ccp.db.migrations import discover_migrations, migrate
from ccp.environment import EnvironmentRecord
from ccp.ingest.app import IngestService
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings, Secret
from ccp.storage import ObjectStore
from ccp.storage.attachments import MIN_EXTERNAL_BYTES, restore_line

pytestmark = pytest.mark.integration

REAL_MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
TOKEN = "capture" + "-client-test-token"  # invented; built from pieces for the secret scanner
SESSION = "sess-capture-1"


# ---------------------------------------------------------------- a server that can restart


class RunningApi:
    """The API served by uvicorn on a fixed local port; can be stopped and started again."""

    def __init__(self, app: Starlette) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self._server.started, "server did not start"

    def stop(self) -> None:
        if self._server and self._thread:
            self._server.should_exit = True
            self._thread.join(timeout=5)
            self._server = self._thread = None


@pytest.fixture
def database(temp_database: DatabaseSettings) -> DatabaseSettings:
    connection = connect(temp_database)
    try:
        migrate(connection, discover_migrations(REAL_MIGRATIONS))
    finally:
        connection.close()
    return temp_database


@pytest.fixture
def api(database: DatabaseSettings, object_store: ObjectStore) -> Iterator[RunningApi]:
    service = IngestService(
        token=Secret(TOKEN),
        store=object_store,
        repository=PostgresRepository(database),
        max_chunk_bytes=8 * 1024 * 1024,
    )
    running = RunningApi(service.build_app())
    running.start()
    try:
        yield running
    finally:
        running.stop()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _client(api: RunningApi, state_dir: Path, **options: Any) -> CaptureClient:
    return CaptureClient(
        HttpApiClient(api.url, Secret(TOKEN), timeout_seconds=5.0),
        StateStore(state_dir),
        lock_wait_seconds=0.5,
        **options,
    )


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


def _stored_text(store: ObjectStore, prefix: str) -> str:
    return "".join(store.get(key).decode() for key in store.list_keys(prefix))


def _attachment_entry(uuid: str, text: str = "x") -> dict[str, Any]:
    body = {"type": "skill_listing", "text": text * (MIN_EXTERNAL_BYTES * 2)}
    return {"type": "attachment", "uuid": uuid, "sessionId": SESSION, "attachment": body}


# ---------------------------------------------------------------- what ends up stored


def test_a_session_is_stored_redacted_with_attachments_once_and_the_log_filled(
    api: RunningApi,
    state_dir: Path,
    tmp_path: Path,
    database: DatabaseSettings,
    object_store: ObjectStore,
    fakes: dict[str, str],
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    _write(
        transcript,
        {
            "type": "user",
            "uuid": "u1",
            "message": f"mail {fakes['email']} key {fakes['aws-access-key-id']}",
        },
        _attachment_entry("a1"),
        _attachment_entry("a2"),  # the same attachment content in a second line
        {"type": "user", "uuid": "u2", "message": f"pan {fakes['pan']}"},
    )

    (result,) = _client(api, state_dir).sync_session(SESSION, transcript)
    assert result.error is None and result.caught_up

    stored = _stored_text(object_store, f"sessions/{SESSION}/main/")
    for secret in (fakes["email"], fakes["aws-access-key-id"], fakes["pan"]):
        assert secret not in stored  # nothing sensitive reached the server
    assert "[REDACTED:email]" in stored
    assert len(object_store.list_keys("attachments/")) == 1  # two lines, one stored body

    assert _rows(database, "SELECT status FROM session") == [("open",)]
    assert _rows(database, "SELECT count(*) FROM job WHERE status = 'queued'") == [(1,)]
    logged = {rule for (rule,) in _rows(database, "SELECT rule_id FROM redaction_log")}
    assert logged == {"email", "aws-access-key-id", "pan"}
    everything = repr(_rows(database, "SELECT * FROM redaction_log"))
    for secret in (fakes["email"], fakes["aws-access-key-id"], fakes["pan"]):
        assert secret not in everything  # the log holds locations, never values


def test_the_stored_transcript_can_be_rebuilt_exactly_from_the_stored_pieces(
    api: RunningApi, state_dir: Path, tmp_path: Path, object_store: ObjectStore
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    entries = [{"type": "user", "uuid": "u1", "message": "plain"}, _attachment_entry("a1")]
    _write(transcript, *entries)
    _client(api, state_dir).sync_session(SESSION, transcript)

    stored_lines = _stored_text(object_store, f"sessions/{SESSION}/main/").splitlines()
    rebuilt = [restore_line(line, object_store) for line in stored_lines]
    assert rebuilt == [_compact(entry) for entry in entries]  # byte for byte


def test_sub_agent_files_arrive_as_their_own_streams(
    api: RunningApi, state_dir: Path, tmp_path: Path, object_store: ObjectStore
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    _write(transcript, {"type": "user", "uuid": "u1"})
    folder = transcript.with_suffix("") / "subagents"
    folder.mkdir(parents=True)
    _write(folder / "agent-a7c5c9.jsonl", {"type": "user", "uuid": "g1", "isSidechain": True})
    (folder / "agent-a7c5c9.meta.json").write_text(
        '{"agentType":"Explore","toolUseId":"t1"}', encoding="utf-8"
    )

    _client(api, state_dir).sync_session(SESSION, transcript)

    keys = object_store.list_keys(f"sessions/{SESSION}/")
    assert sorted(k.split("/")[2] for k in keys) == ["agent-a7c5c9", "agent-a7c5c9.meta", "main"]


# ---------------------------------------------------------------- nothing lost, nothing duplicated


def test_killing_the_api_mid_session_loses_nothing_and_duplicates_nothing(
    api: RunningApi,
    state_dir: Path,
    tmp_path: Path,
    object_store: ObjectStore,
    database: DatabaseSettings,
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    client = _client(api, state_dir)

    _write(transcript, {"type": "user", "uuid": "u1", "message": "first"})
    assert client.sync_session(SESSION, transcript)[0].error is None  # part 1 uploaded

    api.stop()  # the API dies mid-session
    _write(transcript, {"type": "user", "uuid": "u2", "message": "second"}, append=True)
    _write(transcript, _attachment_entry("a1"), append=True)
    during_outage = client.sync_session(SESSION, transcript)[0]
    assert "unavailable" in (during_outage.error or "")  # reported, not raised

    _write(transcript, {"type": "user", "uuid": "u3", "message": "third"}, append=True)
    api.start()  # the API comes back, with the same data stores

    after = client.sync_session(SESSION, transcript)[0]
    assert after.error is None and after.caught_up

    lines = _stored_text(object_store, f"sessions/{SESSION}/main/").splitlines()
    restored = [json.loads(restore_line(line, object_store)) for line in lines]
    assert [e["uuid"] for e in restored] == ["u1", "u2", "a1", "u3"]  # all of it, in order, once
    assert (
        len(object_store.list_keys(f"sessions/{SESSION}/main/")) == 2
    )  # first chunk + one catch-up chunk
    assert _rows(database, "SELECT count(*) FROM session") == [(1,)]


def test_starting_over_after_losing_the_state_folder_creates_no_duplicates(
    api: RunningApi, state_dir: Path, tmp_path: Path, object_store: ObjectStore
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    _write(transcript, {"type": "user", "uuid": "u1"}, _attachment_entry("a1"))
    _client(api, state_dir).sync_session(SESSION, transcript)
    keys_before = object_store.list_keys("")
    stored_before = _stored_text(object_store, "sessions/")

    wiped = tmp_path / "another-state-folder"  # as if the developer's state was deleted
    (result,) = _client(api, wiped).sync_session(SESSION, transcript)

    assert result.error is None  # the server answered "already stored", not a conflict
    assert object_store.list_keys("") == keys_before
    assert _stored_text(object_store, "sessions/") == stored_before


def test_a_wrong_token_is_reported_and_nothing_is_stored(
    api: RunningApi, state_dir: Path, tmp_path: Path, object_store: ObjectStore
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    _write(transcript, {"type": "user", "uuid": "u1"})
    wrong = CaptureClient(
        HttpApiClient(api.url, Secret("wrong-token-value"), 5.0), StateStore(state_dir)
    )
    (result,) = wrong.sync_session(SESSION, transcript)
    assert "401" in (result.error or "")
    assert object_store.list_keys("") == []
    assert StateStore(state_dir).get_offset(SESSION, "main") == 0


# ---------------------------------------------------------------- the command


def test_the_command_uploads_a_session(
    api: RunningApi,
    state_dir: Path,
    tmp_path: Path,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transcript = tmp_path / "projects" / f"{SESSION}.jsonl"
    _write(transcript, {"type": "user", "uuid": "u1"})
    monkeypatch.setenv("CCP_API_URL", api.url)
    monkeypatch.setenv("CCP_INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("CCP_STATE_DIR", str(state_dir))

    assert capture_main(["sync", "--session-id", SESSION, str(transcript)]) == 0

    assert f"{SESSION}: 1 chunk(s), 0 attachment(s) uploaded" in capsys.readouterr().out
    assert len(object_store.list_keys(f"sessions/{SESSION}/")) == 1
    assert capture_main(["catch-up"]) == 0  # and catch-up finds nothing more to send


def test_early_refusals_are_always_seen_as_refusals_never_as_network_errors(
    api: RunningApi,
) -> None:
    """A real bug: replying 401 before reading the request body could reset the connection.

    The client then saw a network error instead of the 401 and treated a permanent failure as a
    temporary outage. Repeated many times, to catch an intermittent problem.
    """
    from ccp.capture.api import ApiRejectedError

    wrong = HttpApiClient(api.url, Secret("wrong-token-value"), 5.0)
    body = _compact({"type": "user", "message": "x" * 5000}).encode() + b"\n"
    for attempt in range(60):
        with pytest.raises(ApiRejectedError) as error:  # not ApiUnavailableError
            wrong.put_chunk(SESSION, "main", attempt, body)
        assert error.value.status == 401
        with pytest.raises(ApiRejectedError) as bad_id:
            HttpApiClient(api.url, Secret(TOKEN), 5.0).put_chunk("bad%20id", "main", 0, body)
        assert bad_id.value.status == 400


# ---------------------------------------------------------------- the environment record


def _environment_rows(database: DatabaseSettings) -> dict[str, Any]:
    """The stored session_env row as a dictionary (empty if there is none)."""
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM session_env")
        rows = cursor.fetchall()
        names = [column[0] for column in cursor.description]
    finally:
        connection.close()
    return dict(zip(names, rows[0], strict=True)) if rows else {}


def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    account = {
        "displayName": "Dev",
        "emailAddress": "dev@example.com",
        "organizationName": "Acme",
        "organizationUuid": "00000000-0000-0000-0000-000000000001",
    }
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": account}), encoding="utf-8")
    return home


HOST = HostInfo("Windows", "11 10.0.26200", "AMD64", "laptop-1", "dev")


def test_the_environment_is_stored_in_two_stages_and_merged(
    api: RunningApi, state_dir: Path, tmp_path: Path, database: DatabaseSettings
) -> None:
    client = _client(api, state_dir)
    start = collect_process_environment(
        {"CLAUDE_CODE_ENTRYPOINT": "cli", "MSYSTEM": "UCRT64"}, _fake_home(tmp_path), HOST
    )

    # Stage 1, at SessionStart: the transcript does not exist yet.
    assert client.report_environment(SESSION, start) is None
    first = _environment_rows(database)
    assert first["entrypoint"] == "cli" and first["hostname"] == "laptop-1"
    assert first["account_email"] == "dev@example.com" and first["auth_type"] == "subscription"
    assert first["claude_code_version"] is None  # not known yet

    # Stage 2, at the first Stop: the transcript says which version and shell.
    transcript = tmp_path / "t.jsonl"
    _write(
        transcript,
        {"type": "user", "version": "2.1.289"},
        {
            "type": "attachment",
            "attachment": {
                "type": "environment",
                "snapshot": {"shell": "PowerShell (primary)", "osVersion": "Windows 11 Home"},
            },
        },
    )
    later = start.merged_with(collect_transcript_environment(transcript))
    assert client.report_environment(SESSION, later) is None

    second = _environment_rows(database)
    assert (
        second["claude_code_version"] == "2.1.289"
        and second["tool_shell"] == "PowerShell (primary)"
    )
    assert second["os_version"] == "Windows 11 Home"  # the more precise transcript value won
    assert second["hostname"] == "laptop-1" and second["entrypoint"] == "cli"  # earlier facts kept
    assert _rows(database, "SELECT count(*) FROM session_env") == [(1,)]
    assert _rows(database, "SELECT status FROM session") == [("open",)]


def test_sending_the_same_environment_again_changes_nothing(
    api: RunningApi, state_dir: Path, database: DatabaseSettings
) -> None:
    record = EnvironmentRecord(surface="terminal", os_name="Windows")
    assert _client(api, state_dir).report_environment(SESSION, record) is None
    before = _environment_rows(database)

    # A fresh client with no memory sends it again: the stored record stays exactly the same.
    fresh_state = state_dir.parent / "fresh-state"
    assert _client(api, fresh_state).report_environment(SESSION, record) is None
    assert _environment_rows(database) == before


def test_a_partial_update_only_changes_the_fields_it_contains(
    api: RunningApi, state_dir: Path, database: DatabaseSettings
) -> None:
    client = _client(api, state_dir)
    client.report_environment(SESSION, EnvironmentRecord(surface="terminal", os_name="Windows"))
    client.report_environment(SESSION, EnvironmentRecord(surface="vscode"))

    row = _environment_rows(database)
    assert (row["surface"], row["os_name"]) == ("vscode", "Windows")


def test_a_malformed_environment_is_refused_and_nothing_is_stored(
    api: RunningApi, database: DatabaseSettings
) -> None:
    import httpx

    url = f"{api.url}/v1/sessions/{SESSION}/environment"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert httpx.put(url, json={"unknown_field": "x"}, headers=headers).status_code == 422
    assert httpx.put(url, json={"surface": "a\nb"}, headers=headers).status_code == 422
    assert _environment_rows(database) == {}
    assert _rows(database, "SELECT count(*) FROM session") == [(0,)]


def test_the_environment_command_records_where_the_session_runs(
    api: RunningApi,
    state_dir: Path,
    tmp_path: Path,
    database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        Path, "home", lambda: _fake_home(tmp_path)
    )  # never read the real login file
    monkeypatch.setenv("CCP_API_URL", api.url)
    monkeypatch.setenv("CCP_INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("CCP_STATE_DIR", str(state_dir))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-vscode")

    assert capture_main(["environment", "--session-id", SESSION]) == 0

    assert f"{SESSION}: environment recorded" in capsys.readouterr().out
    row = _environment_rows(database)
    assert (row["entrypoint"], row["surface"]) == ("claude-vscode", "vscode")
    assert row["account_email"] == "dev@example.com"

    assert capture_main(["environment", "--session-id", SESSION]) == 0  # again: nothing to resend
