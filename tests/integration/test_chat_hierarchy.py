"""The chat hierarchy (migration 016) through the real API, PostgreSQL and MinIO.

Covers what the capture client sends since then: the newer environment fields (which link a chat
to its Claude account and project), session starts and ends (``chat_session``) and tool-result
files; and the views built on them (``chat``, ``d_chat``, ``v_slash_command``, ``v_workflow_run``).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.integration.conftest import API_TOKEN, RunningApi

from ccp.capture.api import ApiRejectedError, HttpApiClient
from ccp.capture.client import CaptureClient
from ccp.capture.state import StateStore
from ccp.db.connection import connect
from ccp.db.labels import set_project_label
from ccp.environment import EnvironmentRecord
from ccp.ingest.repository import PostgresRepository
from ccp.lifecycle import LifecycleEvent
from ccp.settings import DatabaseSettings, Secret
from ccp.storage import ObjectStore

pytestmark = pytest.mark.integration

UUID = "11111111-2222-3333-4444-555555555555"
EMAIL = "dev@" + "example.com"
T0 = datetime(2026, 11, 15, 17, 35, tzinfo=UTC)


def _rows(database: DatabaseSettings, sql: str, *parameters: Any) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql, parameters) if parameters else cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _execute(database: DatabaseSettings, sql: str, *parameters: Any) -> None:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql, parameters) if parameters else cursor.execute(sql)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def http(api: RunningApi) -> HttpApiClient:
    return HttpApiClient(api.url, Secret(API_TOKEN), timeout_seconds=5)


def _environment(**fields: str) -> EnvironmentRecord:
    base = {
        "account_uuid": UUID,
        "account_email": EMAIL,
        "account_name": "Dev",
        "full_name": "Dev Eloper",
        "organisation_role": "admin",
        "hostname": "Laptop-1",
        "folder": "/home/dev/billing",
        "git_remote": "https://user:secret-token@github.com/Acme/Billing.git",
        "git_user_name": "Dev",
        "git_user_email": EMAIL,
    }
    return EnvironmentRecord(**{**base, **fields})


# ---------------------------------------------------------------- account and project


def test_an_environment_record_links_the_chat_to_its_account_and_project(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", _environment())

    assert _rows(
        database,
        "SELECT account_uuid, email, full_name, display_name, organisation_role FROM claude_account",
    ) == [(UUID, EMAIL, "Dev Eloper", "Dev", "admin")]
    assert _rows(
        database, "SELECT project_key, key_kind, name, git_remote, folders FROM project"
    ) == [
        (
            "github.com/acme/billing",
            "git_remote",
            "billing",
            "https://github.com/Acme/Billing.git",
            ["/home/dev/billing"],
        )
    ]
    assert _rows(
        database,
        "SELECT a.account_uuid, p.name, s.git_user_name, s.git_user_email FROM session s "
        "JOIN claude_account a USING (account_id) JOIN project p USING (project_id)",
    ) == [(UUID, "billing", "Dev", EMAIL)]
    stored = _rows(database, "SELECT git_remote FROM session_env")[0][0]
    assert "secret-token" not in stored  # credentials never reach the database


def test_chats_of_one_repository_in_two_folders_are_one_project(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", _environment())
    http.put_environment("chat-1", _environment())  # repeated: nothing new
    http.put_environment("chat-2", _environment(folder="/home/dev/billing-copy"))

    assert _rows(database, "SELECT count(*), max(cardinality(folders)) FROM project") == [(1, 2)]
    assert _rows(database, "SELECT count(*) FROM claude_account") == [(1,)]
    assert _rows(database, "SELECT count(DISTINCT project_id) FROM session") == [(1,)]


def test_a_folder_without_a_remote_is_a_project_of_its_own_machine(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    record = EnvironmentRecord(hostname="Laptop-1", folder="/home/dev/notes", account_email=EMAIL)
    http.put_environment("chat-1", record)
    assert _rows(database, "SELECT project_key, key_kind, name FROM project") == [
        ("laptop-1:/home/dev/notes", "folder", "notes")
    ]


def test_a_folder_labelled_before_any_chat_gives_its_label_to_the_first_chat(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    connection = connect(database)
    try:
        set_project_label(connection, "/srv/newproj", "newlabel")
    finally:
        connection.close()

    http.put_environment("chat-1", EnvironmentRecord(hostname="Box1", folder="/srv/newproj"))

    assert _rows(database, "SELECT project_label FROM chat WHERE chat_id = 'chat-1'") == [
        ("newlabel",)
    ]
    assert _rows(database, "SELECT project_key, label FROM project") == [
        ("box1:/srv/newproj", "newlabel")
    ]


def test_a_project_that_exists_takes_the_label_of_its_folder_if_it_has_none(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", _environment())  # the project github.com/acme/billing
    connection = connect(database)
    try:
        set_project_label(connection, "/home/dev/billing-copy", "billing")  # a folder not seen yet
    finally:
        connection.close()

    http.put_environment("chat-2", _environment(folder="/home/dev/billing-copy"))

    assert _rows(database, "SELECT project_label FROM chat WHERE chat_id = 'chat-1'") == [
        ("billing",)
    ]


def test_an_account_known_by_e_mail_keeps_its_label_when_the_uuid_arrives(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    _execute(database, "INSERT INTO claude_account (email, label) VALUES (%s, 'claude1')", EMAIL)
    http.put_environment("chat-1", _environment())
    assert _rows(database, "SELECT account_uuid, label FROM claude_account") == [(UUID, "claude1")]


def test_a_folder_that_gets_a_git_remote_brings_its_chats_and_label_to_the_git_project(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    # Before: chats in the folder made the folder project laptop-1:/home/dev/billing, labelled.
    folder_only = EnvironmentRecord(hostname="Laptop-1", folder="/home/dev/billing")
    http.put_environment("chat-1", folder_only)
    connection = connect(database)
    try:
        set_project_label(connection, "/home/dev/billing", "billing-api")
    finally:
        connection.close()

    http.put_environment("chat-2", _environment())  # now the folder has an origin remote

    assert _rows(database, "SELECT project_key, key_kind, label, folders FROM project") == [
        ("github.com/acme/billing", "git_remote", "billing-api", ["/home/dev/billing"])
    ]
    assert _rows(database, "SELECT chat_id, project_label FROM chat ORDER BY chat_id") == [
        ("chat-1", "billing-api"),
        ("chat-2", "billing-api"),
    ]


def test_an_old_chat_without_a_remote_stays_in_the_git_project_of_its_folder(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    """The chat was made before the remote was set; indexing it again must not split the project."""
    folder_only = EnvironmentRecord(hostname="Laptop-1", folder="/home/dev/billing")
    http.put_environment("chat-1", folder_only)
    http.put_environment("chat-2", _environment())  # the remote is set: one Git project

    http.put_environment("chat-1", replace(folder_only, git_user_name="Dev"))  # linked again
    http.put_environment("chat-3", folder_only)  # a new chat of a client without the remote

    assert _rows(database, "SELECT project_key, key_kind FROM project") == [
        ("github.com/acme/billing", "git_remote")
    ]
    assert {r[0] for r in _rows(database, "SELECT project_id FROM session")} != {None}
    assert len(_rows(database, "SELECT DISTINCT project_id FROM session")) == 1


def test_a_git_project_with_a_label_of_its_own_keeps_it_when_a_folder_project_joins(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", _environment())
    _execute(database, "UPDATE project SET label = 'git-label'")
    http.put_environment(
        "chat-2", EnvironmentRecord(hostname="Laptop-1", folder="/home/dev/billing-2")
    )
    _execute(database, "UPDATE project SET label = 'folder-label' WHERE key_kind = 'folder'")

    http.put_environment("chat-3", _environment(folder="/home/dev/billing-2"))

    assert _rows(database, "SELECT project_key, label, folders FROM project") == [
        ("github.com/acme/billing", "git-label", ["/home/dev/billing", "/home/dev/billing-2"])
    ]
    assert _rows(database, "SELECT count(DISTINCT project_id) FROM session") == [(1,)]


def test_an_account_uuid_without_an_e_mail_is_given_to_the_chats_labelled_account(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", EnvironmentRecord(account_email=EMAIL))
    _execute(database, "UPDATE claude_account SET label = 'claude1'")

    http.put_environment("chat-1", EnvironmentRecord(account_uuid=UUID))

    assert _rows(database, "SELECT account_uuid, email, label FROM claude_account") == [
        (UUID, EMAIL, "claude1")
    ]
    assert _rows(database, "SELECT account_label FROM chat") == [("claude1",)]


def test_an_older_client_without_an_account_uuid_is_linked_by_e_mail(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", EnvironmentRecord(account_email=EMAIL.upper(), surface="cli"))
    http.put_environment("chat-2", EnvironmentRecord(account_email=EMAIL))
    assert _rows(database, "SELECT account_uuid, email FROM claude_account") == [(None, EMAIL)]
    assert _rows(database, "SELECT count(DISTINCT account_id) FROM session") == [(1,)]


# ---------------------------------------------------------------- sessions of a chat


def _start(key: str, at: datetime, source: str = "startup") -> LifecycleEvent:
    return LifecycleEvent("start", key, at, source=source, commit="ab" * 20)


def _end(key: str, at: datetime, reason: str = "prompt_input_exit") -> LifecycleEvent:
    return LifecycleEvent("end", key, at, reason=reason, commit="cd" * 20)


RUNS_SQL = (
    "SELECT seq, start_source, started_at, ended_at, end_reason, start_commit, end_commit "
    "FROM chat_session ORDER BY seq"
)


def test_a_start_and_its_end_make_one_session_of_the_chat(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.post_lifecycle("chat-1", _start("key-0001", T0))
    http.post_lifecycle("chat-1", _end("key-0001", T0 + timedelta(minutes=30)))
    http.post_lifecycle("chat-1", _end("key-0001", T0 + timedelta(minutes=30)))  # a retry

    assert _rows(database, RUNS_SQL) == [
        (1, "startup", T0, T0 + timedelta(minutes=30), "prompt_input_exit", "ab" * 20, "cd" * 20)
    ]
    assert _rows(
        database, "SELECT start_source, start_commit, end_reason, started_at, ended_at FROM session"
    ) == [("startup", "ab" * 20, "prompt_input_exit", T0, T0 + timedelta(minutes=30))]


def test_a_resumed_chat_gets_a_second_session_and_an_open_one_is_ended(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.post_lifecycle("chat-1", _start("key-0001", T0))
    http.post_lifecycle("chat-1", _start("key-0002", T0 + timedelta(hours=2), "resume"))

    rows = _rows(database, RUNS_SQL)
    assert [(r[0], r[1], r[3], r[4]) for r in rows] == [
        (1, "startup", T0 + timedelta(hours=2), "inferred"),  # no SessionEnd came
        (2, "resume", None, None),
    ]


def test_an_end_that_arrives_before_its_start_is_merged(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.post_lifecycle("chat-1", _end("key-0001", T0 + timedelta(minutes=5), "logout"))
    http.post_lifecycle("chat-1", _start("key-0001", T0, "clear"))
    rows = _rows(database, RUNS_SQL)
    assert [(r[0], r[1], r[4]) for r in rows] == [(1, "clear", "logout")]


def test_a_run_key_of_another_chat_is_refused(http: HttpApiClient) -> None:
    http.post_lifecycle("chat-1", _start("key-0001", T0))
    with pytest.raises(ApiRejectedError) as error:
        http.post_lifecycle("chat-2", _start("key-0001", T0))
    assert error.value.status == 409


def test_the_idle_closer_ends_a_session_that_never_got_its_end(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.post_lifecycle("chat-1", _start("key-0001", datetime.now(UTC) - timedelta(hours=3)))
    _execute(database, "UPDATE session SET last_activity_at = now() - interval '2 hours'")

    assert PostgresRepository(database).close_idle_sessions(30) == ["chat-1"]

    (row,) = _rows(database, "SELECT end_reason, ended_at, (SELECT last_activity_at FROM session) "
                             "FROM chat_session")  # fmt: skip
    assert row[0] == "idle_timeout" and row[1] == row[2]


CHAT_STATE_SQL = "SELECT status, ended_at IS NULL, end_reason FROM session"


def _closed_chat(database: DatabaseSettings, http: HttpApiClient, ended: datetime) -> None:
    """A chat whose only session started and ended, closed by the idle closer since."""
    http.post_lifecycle("chat-1", _start("key-0001", ended - timedelta(minutes=10)))
    http.post_lifecycle("chat-1", _end("key-0001", ended))
    _execute(database, "UPDATE session SET last_activity_at = %s", ended)
    PostgresRepository(database).close_idle_sessions(30)
    assert _rows(database, CHAT_STATE_SQL) == [("closed", False, "prompt_input_exit")]


def test_a_start_that_arrives_late_does_not_reopen_an_ended_chat(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    ended = datetime.now(UTC) - timedelta(hours=2)
    _closed_chat(database, http, ended)

    # A start of an earlier session, sent only now (it waited on the developer's machine).
    http.post_lifecycle("chat-1", _start("key-0000", ended - timedelta(hours=1)))

    assert _rows(database, CHAT_STATE_SQL) == [("closed", False, "prompt_input_exit")]


def test_a_new_start_reopens_the_chat_and_clears_its_end(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    _closed_chat(database, http, datetime.now(UTC) - timedelta(hours=2))

    http.post_lifecycle("chat-1", _start("key-0002", datetime.now(UTC), "resume"))

    assert _rows(database, CHAT_STATE_SQL) == [("open", True, None)]


# ---------------------------------------------------------------- tool-result files


def test_tool_result_files_reach_minio_redacted(
    api: RunningApi,
    database: DatabaseSettings,
    object_store: ObjectStore,
    tmp_path: Path,
    fakes: dict[str, str],
) -> None:
    transcript = tmp_path / "chat-1.jsonl"
    transcript.write_text('{"type":"user","uuid":"u1"}\n', encoding="utf-8")
    folder = tmp_path / "chat-1" / "tool-results"
    folder.mkdir(parents=True)
    (folder / "bs12jynbi.txt").write_text(f"big output {fakes['email']}\n", encoding="utf-8")
    client = CaptureClient(
        HttpApiClient(api.url, Secret(API_TOKEN), timeout_seconds=5), StateStore(tmp_path / "s")
    )

    results = client.sync_session("chat-1", transcript)

    assert results[-1].files == 1 and results[-1].error is None
    stored = object_store.get("sessions/chat-1/tool-results/bs12jynbi.txt").decode()
    assert stored == "big output [REDACTED:email]\n"
    assert _rows(database, "SELECT rule_id, location FROM redaction_log") == [
        ("email", "sessions/chat-1/tool-results/bs12jynbi.txt:0:")
    ]
    assert "sessions/chat-1/main/000000000000.jsonl" in object_store.list_keys("sessions/chat-1/")


# ---------------------------------------------------------------- the views


def _event(database: DatabaseSettings, chat: str, line: int, event_type: str, payload: Any) -> int:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO event (session_id, event_type, actor, occurred_at, payload, "
            "raw_object_key, raw_line_number) VALUES (%s, %s, 'human', %s, %s, %s, %s) "
            "RETURNING event_id",
            (chat, event_type, T0, json.dumps(payload), f"sessions/{chat}/main/0.jsonl", line),
        )
        (event_id,) = cursor.fetchone()
        # What the indexer does once a chat holds a message (a chat first seen through its
        # environment record starts as empty).
        cursor.execute("UPDATE session SET is_empty = FALSE WHERE session_id = %s", (chat,))
        connection.commit()
        return int(event_id)
    finally:
        connection.close()


def test_the_chat_view_and_d_chat(http: HttpApiClient, database: DatabaseSettings) -> None:
    http.put_environment("chat-1", _environment())
    http.post_lifecycle("chat-1", _start("key-0001", T0))
    http.put_environment("chat-empty", _environment())
    _execute(database, "UPDATE session SET is_empty = TRUE WHERE session_id = 'chat-empty'")
    _execute(database, "UPDATE claude_account SET label = 'claude1'")
    _event(database, "chat-1", 1, "prompt", {"cwd": "/home/dev/billing"})

    assert _rows(
        database,
        "SELECT chat_id, account_email, account_label, project_name, folder, session_count "
        "FROM chat ORDER BY chat_id",
    ) == [
        ("chat-1", EMAIL, "claude1", "billing", "/home/dev/billing", 1),
        ("chat-empty", EMAIL, "claude1", "billing", "/home/dev/billing", 0),
    ]
    assert _rows(database, "SELECT chat_id, account, project, sessions, prompts FROM d_chat") == [
        ("chat-1", "claude1", "billing", 1, 1)  # the empty chat is left out
    ]


def test_d_chat_shows_the_highest_effort(http: HttpApiClient, database: DatabaseSettings) -> None:
    http.put_environment("chat-1", _environment())
    first = _event(database, "chat-1", 1, "response", {})
    second = _event(database, "chat-1", 2, "response", {})
    for message_id, event_id, effort in (("m1", first, "xhigh"), ("m2", second, "medium")):
        _execute(
            database,
            "INSERT INTO model_usage (session_id, message_id, event_id, model, effort) "
            "VALUES ('chat-1', %s, %s, 'model-a', %s)",
            message_id,
            event_id,
            effort,
        )
    assert _rows(database, "SELECT effort FROM d_chat") == [("xhigh",)]


def test_slash_commands_are_split_into_name_and_arguments(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", _environment())
    text = "<command-name>/model</command-name>\n<command-message>model</command-message>\n"
    _event(database, "chat-1", 1, "slash_command", {"message": {"content": text + "<command-args>opus</command-args>"}})  # fmt: skip
    blocks = [{"type": "text", "text": "<command-name>/compact</command-name>"}]
    _event(database, "chat-1", 2, "slash_command", {"message": {"content": blocks}})
    assert _rows(database, "SELECT name, arguments FROM v_slash_command ORDER BY event_id") == [
        ("/model", "opus"),
        ("/compact", None),
    ]


def test_a_workflow_run_joins_its_agents_and_its_launching_call(
    http: HttpApiClient, database: DatabaseSettings
) -> None:
    http.put_environment("chat-1", _environment())
    call = _event(database, "chat-1", 1, "tool_call", {})
    result = _event(
        database,
        "chat-1",
        2,
        "tool_result",
        {"toolUseResult": {"runId": "wf_1", "workflowName": "review", "status": "completed"}},
    )
    _execute(database, "UPDATE event SET tool_use_ids = ARRAY['toolu_1']")
    _execute(
        database,
        "INSERT INTO agent_run (session_id, agent_id, workflow_run_id, event_count) "
        "VALUES ('chat-1', 'a1', 'wf_1', 3), ('chat-1', 'a2', 'wf_1', 4)",
    )
    assert _rows(
        database,
        "SELECT workflow_run_id, workflow_name, status, agent_count, event_count, call_event_id, "
        "result_event_id FROM v_workflow_run",
    ) == [("wf_1", "review", "completed", 2, 7, call, result)]
