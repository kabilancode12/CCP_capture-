"""The catalog of an invented chat (real PostgreSQL and MinIO, see conftest.py).

The chat is the story of tests/integration/story.py, indexed by the real indexer. The facts that
the indexer of another stage fills (account, project, title, sessions, decisions, test cases,
corrections) are inserted here with plain SQL, so these tests need only the schema.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from tests.integration.story import SESSION, STORY, SUB_AGENT, as_chunk, index_story, make_prompt

from ccp.catalog import CatalogError, catalog_all, catalog_chat
from ccp.catalog.cli import tree_lines
from ccp.db.connection import connect
from ccp.index.repository import IndexRepository
from ccp.index.worker import process_next_job
from ccp.ingest.repository import PostgresRepository
from ccp.settings import DatabaseSettings
from ccp.storage import ObjectStore
from ccp.storage.keys import transcript_chunk_key

pytestmark = pytest.mark.integration

ACCOUNT_UUID = "11111111-2222-3333-4444-555555555555"
BASE = "catalog/accounts/claude1__11111111/projects/billing/chats"
CHAT = f"{BASE}/2026-07-01__fix-the-bug__sess-sto"
OTHER = "sess-stor2"  # starts with the same 8 characters as SESSION


@pytest.fixture
def connection(database: DatabaseSettings) -> Iterator[Any]:
    conn = connect(database)
    try:
        yield conn
    finally:
        conn.close()


def _run(conn: Any, sql: str, *params: Any) -> None:
    conn.cursor().execute(sql, params)
    conn.commit()


def _one(conn: Any, sql: str, *params: Any) -> Any:
    cursor = conn.cursor()
    cursor.execute(sql, params)
    value = cursor.fetchone()[0]
    conn.rollback()
    return value


def _describe_chat(conn: Any, session_id: str, title: str) -> None:
    """Account, project, title and the facts later stages fill, for one indexed chat."""
    if _one(conn, "SELECT count(*) FROM claude_account WHERE account_uuid = %s", ACCOUNT_UUID) == 0:
        _run(
            conn,
            "INSERT INTO claude_account (account_uuid, email, display_name, label) "
            "VALUES (%s, 'dev@example.org', 'Dev One', 'claude1')",
            ACCOUNT_UUID,
        )
        _run(
            conn,
            "INSERT INTO project (project_key, key_kind, name, git_remote, folders) VALUES "
            "('github.com/acme/billing', 'git_remote', 'billing', 'github.com/acme/billing', "
            "ARRAY['/work/app'])",
        )
    _run(
        conn,
        "UPDATE session SET "
        "account_id = (SELECT account_id FROM claude_account WHERE account_uuid = %s), "
        "project_id = (SELECT project_id FROM project WHERE project_key = %s), title = %s, is_empty = FALSE, "
        "started_at = '2026-07-01T10:00:00Z' WHERE session_id = %s",
        ACCOUNT_UUID,
        "github.com/acme/billing",
        title,
        session_id,
    )


def _facts_of_story(conn: Any) -> None:
    # What the indexer may already have filled is replaced, so the expectations below are exact.
    _run(conn, "DELETE FROM chat_session WHERE session_id = %s", SESSION)
    _run(conn, "DELETE FROM correction WHERE session_id = %s", SESSION)
    _run(
        conn,
        "DELETE FROM test_case WHERE test_run_id IN "
        "(SELECT test_run_id FROM test_run WHERE session_id = %s)",
        SESSION,
    )
    _run(
        conn,
        "INSERT INTO chat_session (session_id, seq, start_source, started_at, ended_at, end_reason) "
        "VALUES (%s, 1, 'startup', '2026-07-01T10:00:00Z', '2026-07-01T10:20:00Z', 'other'), "
        "(%s, 2, 'inferred', '2026-07-01T11:00:00Z', NULL, NULL)",
        SESSION,
        SESSION,
    )
    _run(
        conn,
        "INSERT INTO decision (session_id, seq, title, goal, options, status, reviewed_by_kind, "
        "reviewer) VALUES (%s, 1, 'Use a.py', 'Fix it', '[\"a\", \"b\"]', 'approved', 'ai', 'm')",
        SESSION,
    )
    _run(
        conn,
        "INSERT INTO test_case (test_run_id, position, name, outcome, message) "
        "SELECT min(test_run_id), 1, 'test_login', 'failed', 'assert 1 == 2' FROM test_run "
        "WHERE session_id = %s",
        SESSION,
    )
    _run(
        conn,
        "INSERT INTO correction (session_id, kind, corrected_by, trigger_event_id, detected_by) "
        "SELECT session_id, 'test_fix', 'test', min(event_id), 'rule' FROM event "
        "WHERE session_id = %s GROUP BY session_id",
        SESSION,
    )
    _run(
        conn,
        "INSERT INTO commit_link (commit_sha, session_id, method, confidence, matched_lines, "
        "total_lines) VALUES ('abc1234', %s, 'hunk_match', 0.9, 3, 3)",
        SESSION,
    )


@pytest.fixture
def story(database: DatabaseSettings, object_store: ObjectStore, connection: Any) -> ObjectStore:
    index_story(database, object_store)
    _describe_chat(connection, SESSION, "Fix the bug")
    _facts_of_story(connection)
    return object_store


def _json(store: ObjectStore, key: str) -> Any:
    return json.loads(store.get(key))


def _raw_objects(store: ObjectStore) -> dict[str, bytes]:
    return {k: store.get(k) for k in store.list_keys("sessions/")}


def _catalog_keys(store: ObjectStore) -> list[str]:
    return store.list_keys("catalog/")


def test_chat_folder_has_every_file(story: ObjectStore, connection: Any) -> None:
    assert catalog_chat(connection, story, SESSION) == CHAT
    names = {k.removeprefix(f"{CHAT}/") for k in story.list_keys(f"{CHAT}/")}
    assert names == {
        "README.md", "00_summary.json", "01_conversation.md", "02_sessions.json",
        "03_decisions.json", "04_tests.json", "05_code_changes.json", "06_corrections.json",
        "07_subagents_and_workflows.json", "08_usage.json", "transcript/main.json",
        "transcript/agents/explore__a1.json",
    }  # fmt: skip


def test_summary_and_fact_files_match_the_database(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    summary = _json(story, f"{CHAT}/00_summary.json")
    assert summary["chat_id"] == SESSION and summary["title"] == "Fix the bug"
    assert summary["account"]["label"] == "claude1" and summary["project"]["name"] == "billing"
    assert summary["counts"]["prompts"] == 2  # p1 and p3; p2 was rewound, the summary is no prompt
    assert summary["counts"]["file_changes"] == 2 and summary["counts"]["sessions"] == 2
    assert summary["counts"]["test_runs"] == 2 and summary["counts"]["commits"] == 1
    assert summary["models"] == ["helper-model", "model-a"] and summary["cost_usd"] == 1.5
    assert "dev@example.org" not in story.get(f"{CHAT}/00_summary.json").decode()

    assert [s["seq"] for s in _json(story, f"{CHAT}/02_sessions.json")] == [1, 2]
    (decision,) = _json(story, f"{CHAT}/03_decisions.json")
    assert decision["options"] == ["a", "b"] and decision["reviewed_by_kind"] == "ai"
    runs = _json(story, f"{CHAT}/04_tests.json")["runs"]
    assert runs[0]["passed"] is False and runs[0]["cases"][0]["name"] == "test_login"
    assert runs[1]["passed"] is True and runs[1]["cases"] == []
    changes = _json(story, f"{CHAT}/05_code_changes.json")
    assert changes["summary"]["changes"] == 2 and changes["commits"][0]["commit_sha"] == "abc1234"
    assert changes["changes"][0]["raw"]["key"].startswith(f"sessions/{SESSION}/main/")
    (correction,) = _json(story, f"{CHAT}/06_corrections.json")
    assert correction["kind"] == "test_fix" and correction["trigger"]["line"] >= 1
    agents = _json(story, f"{CHAT}/07_subagents_and_workflows.json")["agents"]
    assert [a["agent_type"] for a in agents] == ["Explore"]
    usage = _json(story, f"{CHAT}/08_usage.json")
    assert (
        usage["total"]["cost_usd"] == 1.5 and usage["cost_by_model"][0]["model"] == "helper-model"
    )


def test_conversation_is_readable(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    text = story.get(f"{CHAT}/01_conversation.md").decode()
    assert text.startswith("# Fix the bug")
    assert "> Fix the bug in a.py" in text and "Tests pass now." in text
    assert "> *Thinking:* Let me check." in text
    assert "- **Bash** `pytest -q`\n  - result: error: Exit code 1 2 failed" in text
    assert "- **Bash** `rm -rf build`" in text and "(rewound)" in text
    assert "*The conversation was compacted (auto, 9000 to 1200 tokens).*" in text
    assert "> Add a feature" in text and "(rewound)" in text.split("> Add a feature")[0][-60:]
    assert "SUMMARY" not in text  # the compaction summary is not a prompt


def test_transcripts_have_one_item_per_raw_line(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    main = _json(story, f"{CHAT}/transcript/main.json")
    assert main == STORY  # same objects, same order
    assert story.get(f"{CHAT}/transcript/main.json").decode().startswith("[\n  {\n    ")  # indent 2
    assert _json(story, f"{CHAT}/transcript/agents/explore__a1.json") == SUB_AGENT


def test_index_and_readmes(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    index = _json(story, "catalog/index.json")
    (account,) = index["accounts"]
    (project,) = account["projects"]
    assert account["name"] == "claude1" and account["path"].endswith("claude1__11111111")
    assert project["name"] == "billing" and project["path"] == f"{account['path']}/projects/billing"
    assert project["chats"][0]["chat_id"] == SESSION and project["chats"][0]["path"] == CHAT
    assert "accounts/<account>__<id8>/" in story.get("catalog/README.md").decode()
    assert "01_conversation.md" in story.get(f"{CHAT}/README.md").decode()
    assert tree_lines(_catalog_keys(story), with_files=False)[-1].endswith("__sess-sto")


def test_raw_objects_are_never_touched_and_a_rebuild_is_identical(
    story: ObjectStore, connection: Any
) -> None:
    raw = _raw_objects(story)
    catalog_chat(connection, story, SESSION)
    first = {k: story.get(k) for k in _catalog_keys(story)}
    catalog_chat(connection, story, SESSION)
    assert {k: story.get(k) for k in _catalog_keys(story)} == first
    assert _raw_objects(story) == raw


def test_a_changed_title_or_label_moves_the_folder(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    _run(connection, "UPDATE session SET title = 'A new title' WHERE session_id = %s", SESSION)
    assert catalog_chat(connection, story, SESSION) == f"{BASE}/2026-07-01__a-new-title__sess-sto"
    assert story.list_keys(f"{CHAT}/") == []  # the old folder is gone

    _run(
        connection,
        "UPDATE claude_account SET label = 'Claude Two' WHERE account_uuid = %s",
        ACCOUNT_UUID,
    )
    _run(
        connection,
        "UPDATE project SET label = 'Billing API' WHERE project_key = %s",
        "github.com/acme/billing",
    )
    moved = catalog_chat(connection, story, SESSION)
    assert moved == (
        "catalog/accounts/claude-two__11111111/projects/billing-api/chats/"
        "2026-07-01__a-new-title__sess-sto"
    )
    assert [k for k in _catalog_keys(story) if "claude1__" in k or "/billing/" in k] == []
    assert story.exists(f"{moved}/00_summary.json")
    (account,) = _json(story, "catalog/index.json")["accounts"]
    assert account["name"] == "Claude Two"


def test_files_of_an_earlier_build_are_removed(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    story.put(f"{CHAT}/transcript/agents/old-name__a1.json", b"[]")
    catalog_chat(connection, story, SESSION)
    assert not story.exists(f"{CHAT}/transcript/agents/old-name__a1.json")


def test_an_empty_chat_has_no_folder(story: ObjectStore, connection: Any) -> None:
    catalog_chat(connection, story, SESSION)
    _run(connection, "UPDATE session SET is_empty = TRUE WHERE session_id = %s", SESSION)
    assert catalog_chat(connection, story, SESSION) is None
    assert [k for k in _catalog_keys(story) if "/chats/" in k] == []
    assert _json(story, "catalog/index.json") == {"accounts": []}
    result = catalog_all(connection, story)
    assert result.built == [] and result.failed == {}


def test_unknown_chat_is_an_error(story: ObjectStore, connection: Any) -> None:
    with pytest.raises(CatalogError):
        catalog_chat(connection, story, "no-such-chat")


def _index_small_chat(database: DatabaseSettings, store: ObjectStore, session_id: str) -> None:
    lines = [make_prompt("p1", None, 0, "Hello"), make_prompt("p2", "p1", 5, "More")]
    store.put(transcript_chunk_key(session_id, "main", 0), as_chunk(lines))
    PostgresRepository(database).record_chunk(session_id)
    assert process_next_job(store, IndexRepository(database)) is True


def test_catalog_all_with_chats_that_share_an_id_start_and_one_without_account(
    story: ObjectStore, connection: Any, database: DatabaseSettings
) -> None:
    _index_small_chat(database, story, OTHER)
    _run(
        connection,
        "UPDATE session SET title = 'Other chat', is_empty = FALSE, account_id = NULL, project_id = NULL, "
        "started_at = '2026-07-03T09:00:00Z' WHERE session_id = %s",
        OTHER,
    )  # no account; the project is found by the folder
    result = catalog_all(connection, story)
    other = "catalog/accounts/unknown-account__00000000/projects/billing/chats"
    assert sorted(result.built) == [CHAT, f"{other}/2026-07-03__other-chat__sess-sto"]

    # Renaming one chat must not remove the other, although both folders end in "__sess-sto".
    _run(
        connection, "UPDATE session SET title = 'Fix the bug again' WHERE session_id = %s", SESSION
    )
    catalog_chat(connection, story, SESSION)
    assert story.exists(f"{other}/2026-07-03__other-chat__sess-sto/00_summary.json")
    assert not story.exists(f"{CHAT}/00_summary.json")
    assert story.exists(f"{BASE}/2026-07-01__fix-the-bug-again__sess-sto/00_summary.json")
    index = _json(story, "catalog/index.json")
    assert [a["path"].split("/")[-1] for a in index["accounts"]] == [
        "claude1__11111111",
        "unknown-account__00000000",
    ]
