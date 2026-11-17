"""Rebuilding the index from the stored transcripts gives identical rows (real PostgreSQL and MinIO)."""

from pathlib import Path
from typing import Any

import pytest
from tests.integration.story import SESSION, index_story

from ccp.db.connection import connect
from ccp.index.cli import main
from ccp.index.rebuild import RebuildRepository, rebuild_session
from ccp.index.repository import IndexRepository
from ccp.index.worker import index_session
from ccp.settings import DatabaseSettings, StorageSettings
from ccp.storage import ObjectStore

pytestmark = pytest.mark.integration

MIGRATIONS = Path(__file__).parents[2] / "db" / "migrations"
TABLES = (
    "event",
    "thinking",
    "model_usage",
    "session_cost",
    "session_model_cost",
    "compaction",
    "agent_run",
    "file_change",
    "test_run",
)


def _rows(database: DatabaseSettings, sql: str) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def _execute(database: DatabaseSettings, sql: str) -> None:
    connection = connect(database)
    try:
        connection.cursor().execute(sql)
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def story(database: DatabaseSettings, object_store: ObjectStore) -> DatabaseSettings:
    index_story(database, object_store)
    return database


def test_the_story_fills_every_derived_table(story: DatabaseSettings) -> None:
    """Guards the other tests: an empty table would make 'identical' meaningless."""
    for table in TABLES:
        assert _rows(story, f"SELECT count(*) FROM {table}")[0][0] > 0, table  # noqa: S608


def test_rebuilding_from_the_stored_transcripts_gives_identical_rows(
    story: DatabaseSettings, object_store: ObjectStore
) -> None:
    rebuild = RebuildRepository(story)
    before = rebuild.fingerprints(SESSION)

    result = rebuild_session(object_store, IndexRepository(story), rebuild, SESSION, check=True)

    assert result is not None
    assert result.differences == ()
    assert result.events > 0
    assert rebuild.fingerprints(SESSION) == before


def test_the_rebuild_really_deleted_and_rebuilt_the_rows(
    story: DatabaseSettings, object_store: ObjectStore
) -> None:
    old_ids = _rows(story, "SELECT min(event_id) FROM event")[0][0]

    rebuild_session(
        object_store, IndexRepository(story), RebuildRepository(story), SESSION, check=True
    )

    # The database's own ids are new: the rows were rebuilt, not left alone.
    assert _rows(story, "SELECT min(event_id) FROM event")[0][0] > old_ids


def test_the_fingerprint_does_not_depend_on_the_databases_own_ids(
    story: DatabaseSettings, object_store: ObjectStore
) -> None:
    rebuild = RebuildRepository(story)
    first = rebuild.fingerprints(SESSION)
    rebuild.reset(SESSION)

    index_session(object_store, IndexRepository(story), SESSION)

    assert rebuild.fingerprints(SESSION) == first


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("event", "UPDATE event SET event_type = 'changed' WHERE raw_line_number = 1"),
        ("file_change", "UPDATE file_change SET lines_added = lines_added + 1"),
        ("test_run", "UPDATE test_run SET passed = NOT passed"),
        ("model_usage", "DELETE FROM model_usage"),
        ("session_cost", "UPDATE session_cost SET total_cost_usd = 0"),
        ("agent_run", "UPDATE agent_run SET result = 'other'"),
        ("compaction", "UPDATE compaction SET pre_tokens = 1"),
        ("session", "UPDATE session SET branch = 'elsewhere'"),
    ],
)
def test_the_fingerprint_notices_a_change_in_each_table(
    story: DatabaseSettings, table: str, sql: str
) -> None:
    rebuild = RebuildRepository(story)
    before = rebuild.fingerprints(SESSION)

    _execute(story, sql)

    changed = [t for t, value in rebuild.fingerprints(SESSION).items() if value != before[t]]
    assert table in changed


def test_a_rebuild_repairs_a_session_whose_index_was_damaged(
    story: DatabaseSettings, object_store: ObjectStore
) -> None:
    rebuild = RebuildRepository(story)
    good = rebuild.fingerprints(SESSION)
    _execute(story, "UPDATE file_change SET lines_added = 99")
    _execute(story, "DELETE FROM test_run")

    result = rebuild_session(object_store, IndexRepository(story), rebuild, SESSION, check=True)

    assert result is not None
    assert set(result.differences) == {"file_change", "test_run"}  # what was damaged is reported
    assert rebuild.fingerprints(SESSION) == good  # and is repaired


def test_without_check_the_index_is_deleted_and_a_job_is_queued(
    story: DatabaseSettings, object_store: ObjectStore
) -> None:
    _execute(story, "UPDATE job SET status = 'done'")

    result = rebuild_session(
        object_store, IndexRepository(story), RebuildRepository(story), SESSION, check=False
    )

    assert result is None
    assert _rows(story, "SELECT count(*) FROM event")[0][0] == 0
    assert _rows(story, "SELECT status FROM job WHERE status = 'queued'") == [("queued",)]


def test_a_chat_without_a_transcript_keeps_the_start_time_its_session_start_gave(
    database: DatabaseSettings, object_store: ObjectStore
) -> None:
    # An empty chat (opened and closed in VS Code without a message) has no events: its start time
    # comes from the SessionStart hook only (table chat_session), and a rebuild must keep it.
    _execute(
        database,
        "INSERT INTO session (session_id, started_at) VALUES ('empty', '2026-11-15 09:00+00')",
    )
    _execute(
        database,
        "INSERT INTO chat_session (session_id, seq, start_source, started_at, run_key) VALUES "
        "('empty', 1, 'startup', '2026-11-15 09:00+00', 'k1'), "
        "('empty', 2, 'resume', '2026-11-15 10:00+00', 'k2')",
    )
    rebuild = RebuildRepository(database)
    before = rebuild.fingerprints("empty")

    result = rebuild_session(object_store, IndexRepository(database), rebuild, "empty", check=True)

    assert result is not None and result.differences == ()
    assert rebuild.fingerprints("empty") == before


def test_other_sessions_and_the_capture_data_are_not_touched(
    story: DatabaseSettings, object_store: ObjectStore
) -> None:
    _execute(story, "INSERT INTO session (session_id) VALUES ('other')")
    _execute(
        story,
        "INSERT INTO event (session_id, event_type, actor, payload, raw_object_key, raw_line_number) "
        "VALUES ('other', 'x', 'system', '{}', 'sessions/other/main/0', 1)",
    )
    _execute(story, "INSERT INTO session_env (session_id, surface) VALUES ('sess-story', 'vscode')")
    _execute(
        story,
        "INSERT INTO redaction_log (session_id, rule_id, location) "
        "VALUES ('sess-story', 'email', 'x')",
    )

    rebuild_session(
        object_store, IndexRepository(story), RebuildRepository(story), SESSION, check=True
    )

    assert _rows(story, "SELECT count(*) FROM event WHERE session_id = 'other'") == [(1,)]
    assert _rows(story, "SELECT surface FROM session_env") == [("vscode",)]
    assert _rows(story, "SELECT count(*) FROM redaction_log") == [(1,)]


# ---------------------------------------------------------------------------- the command


@pytest.fixture
def environment(
    story: DatabaseSettings,
    storage_settings: StorageSettings,
    object_store: ObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the command at the throwaway database and bucket."""
    values = {
        "POSTGRES_HOST": story.host,
        "POSTGRES_PORT": str(story.port),
        "POSTGRES_DB": story.name,
        "POSTGRES_USER": story.user,
        "POSTGRES_PASSWORD": story.password.reveal(),
        "MINIO_ENDPOINT": storage_settings.endpoint,
        "MINIO_ACCESS_KEY": storage_settings.access_key,
        "MINIO_SECRET_KEY": storage_settings.secret_key.reveal(),
        "MINIO_BUCKET": object_store._bucket,  # noqa: SLF001  (the fixture's throwaway bucket)
        "MINIO_SECURE": "true" if storage_settings.secure else "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _command(*arguments: str) -> int:
    return main([*arguments, "--migrations-dir", str(MIGRATIONS)])


def test_the_command_checks_one_session(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _command("rebuild", "--session", SESSION, "--check")

    out = capsys.readouterr().out
    assert code == 0
    assert f"{SESSION}: identical" in out
    assert "1 of 1 session(s) identical" in out


def test_the_command_checks_every_session(
    environment: None, story: DatabaseSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    _execute(story, "INSERT INTO session (session_id) VALUES ('empty-session')")

    code = _command("rebuild", "--all", "--check")

    out = capsys.readouterr().out
    assert code == 0
    assert "empty-session: identical (0 events)" in out
    assert "2 of 2 session(s) identical" in out


def test_the_command_exits_with_1_when_a_rebuild_differs(
    environment: None,
    story: DatabaseSettings,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Make the rebuild differ: right after indexing, the model usage rows disappear.
    real = index_session

    def lossy(store: Any, repository: Any, session_id: str) -> Any:
        result = real(store, repository, session_id)
        _execute(story, "DELETE FROM model_usage")
        return result

    monkeypatch.setattr("ccp.index.rebuild.index_session", lossy)

    code = _command("rebuild", "--session", SESSION, "--check")

    assert code == 1
    assert "DIFFERENT in model_usage" in capsys.readouterr().out


def test_the_command_refuses_an_unknown_session(
    environment: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _command("rebuild", "--session", "no-such-session", "--check")

    assert code == 1
    assert "Unknown session(s): no-such-session" in capsys.readouterr().err


def test_the_command_without_check_only_queues(
    environment: None, story: DatabaseSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _command("rebuild", "--session", SESSION)

    assert code == 0
    assert "index deleted, job queued" in capsys.readouterr().out
    assert _rows(story, "SELECT count(*) FROM event")[0][0] == 0


def test_a_queued_rebuild_is_completed_by_the_worker(
    environment: None, story: DatabaseSettings
) -> None:
    rebuild = RebuildRepository(story)
    before = rebuild.fingerprints(SESSION)
    assert _command("rebuild", "--session", SESSION) == 0

    assert _command("run", "--once") == 0

    assert rebuild.fingerprints(SESSION) == before
