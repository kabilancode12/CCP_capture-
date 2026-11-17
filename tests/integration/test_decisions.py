"""Decisions through the real database: digest, extraction, review, storage, override, re-run policy.

The chat is invented and its rows are inserted directly (the indexer is not involved). The model is a
fake provider that answers from the digest it is shown: no real model is ever called.
"""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from ccp.db.connection import connect
from ccp.decisions import prompts
from ccp.decisions.cli import main as decisions_main
from ccp.decisions.digest import build_digest
from ccp.decisions.provider import ProviderError
from ccp.decisions.repository import (
    DecisionError,
    chat_state,
    pending_chats,
    set_decision_status,
)
from ccp.decisions.runner import process_chat
from ccp.decisions.settings import DecisionSettings
from ccp.settings import DatabaseSettings

pytestmark = pytest.mark.integration

CHAT = "chat-decisions-1"
KEY = f"sessions/{CHAT}/main/000000000000.jsonl"
SHA = "0123456789abcdef0123456789abcdef01234567"
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
SETTINGS = DecisionSettings(model="test-model", review_model="test-reviewer", max_chars=80_000)


class FakeProvider:
    """Answers extraction and review from the digest it is shown, and records every call."""

    def __init__(self, *, extract: Any = None, review: Any = None, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._extract = extract
        self._review = review
        self._fail = fail

    def complete(self, system: str, prompt: str, model: str) -> str:
        self.calls.append((system, prompt, model))
        if self._fail:
            raise ProviderError("the model is down")
        if system == prompts.EXTRACT_SYSTEM:
            return (
                self._extract
                if isinstance(self._extract, str)
                else json.dumps(self._extract or default_extraction(prompt))
            )
        assert system == prompts.REVIEW_SYSTEM
        return json.dumps(
            self._review
            or {
                "reviews": [
                    {"index": 0, "verdict": "approve", "note": "supported"},
                    {"index": 1, "verdict": "needs_review", "note": "result not shown"},
                ]
            }
        )


def line_of(digest_text: str, label: str, containing: str = "") -> int:
    """The digest line number of the first line with this label (and text)."""
    for line in digest_text.splitlines():
        match = re.match(rf"\[L(\d+)\] {label}\b", line)
        if match and containing in line:
            return int(match.group(1))
    raise AssertionError(f"no {label} line with {containing!r}")


def default_extraction(prompt: str) -> dict[str, Any]:
    first = line_of(prompt, "PROMPT", "Add a queue")
    fix = line_of(prompt, "PROMPT", "tabs")
    edit = line_of(prompt, "EDIT")
    test = line_of(prompt, "TEST", "PASSED")
    return {
        "decisions": [
            {
                "title": "Use a queue",
                "goal": "decouple the sender",
                "options": ["queue", "thread"],
                "chosen": "queue",
                "reason": "simple and testable",
                "result": "works",
                "tests": "2 passed after one failure",
                "decided_by": "claude",
                "file_lines": [edit],
                "commit_shas": [SHA[:8]],
                "evidence_lines": [first, edit, test],
            },
            {
                "title": "Indent with tabs",
                "goal": "match the project style",
                "options": [],
                "chosen": "tabs",
                "reason": "the developer asked",
                "result": "done",
                "tests": None,
                "decided_by": "developer",
                "file_lines": [9999],
                "commit_shas": [],
                "evidence_lines": [fix, 9999],
            },
        ],
        "developer_corrections": [{"line": fix, "note": "use tabs, not spaces"}],
    }


@pytest.fixture
def conn(database: DatabaseSettings) -> Iterator[Any]:
    """A connection to a migrated throwaway database holding one invented chat."""
    connection = connect(database)
    seed_chat(connection)
    try:
        yield connection
    finally:
        connection.close()


def add_event(
    cursor: Any,
    line: int,
    event_type: str,
    actor: str,
    minute: int,
    payload: dict[str, Any],
    chat: str = CHAT,
) -> int:
    cursor.execute(
        "INSERT INTO event (session_id, event_type, actor, occurred_at, payload, raw_object_key,"
        " raw_line_number) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING event_id",
        (
            chat,
            event_type,
            actor,
            T0 + timedelta(minutes=minute),
            json.dumps(payload),
            f"sessions/{chat}/main/000000000000.jsonl",
            line,
        ),
    )
    return int(cursor.fetchone()[0])


def text_message(text: str) -> dict[str, Any]:
    return {"message": {"content": [{"type": "text", "text": text}]}}


def seed_chat(connection: Any, chat: str = CHAT) -> None:
    cursor = connection.cursor()
    add = partial(add_event, cursor, chat=chat)
    cursor.execute(
        "INSERT INTO session (session_id, title, started_at, branch, is_empty)"
        " VALUES (%s, 'Queue work', %s, 'main', FALSE)",
        (chat, T0),
    )
    add(1, "prompt", "human", 0, {"message": {"content": "Add a queue to the sender"}})
    add(2, "response", "ai", 1, text_message("I will add a queue."))
    edit = add(3, "tool_result", "system", 2, {})
    call = add(4, "tool_call", "ai", 3, {})
    cursor.execute(
        "INSERT INTO file_change (session_id, event_id, path, absolute_path, change_type,"
        " lines_added, lines_removed, file_role) VALUES (%s, %s, 'src/queue.py', '/w/src/queue.py',"
        " 'added', 20, 0, 'code')",
        (chat, edit),
    )
    cursor.execute(
        "INSERT INTO test_run (session_id, call_event_id, tool_use_id, command, framework, passed,"
        " tests_passed, tests_failed, tests_skipped) VALUES (%s, %s, 't1', 'pytest -q', 'pytest',"
        " TRUE, 2, 0, 0)",
        (chat, call),
    )
    add(5, "prompt", "human", 5, {"message": {"content": "Use tabs, not spaces"}})
    add(6, "response", "ai", 6, text_message("Switched to tabs."))
    cursor.execute(
        "INSERT INTO commit_link (commit_sha, session_id, method, confidence, matched_lines,"
        " total_lines, committed_at) VALUES (%s, %s, 'hunk_match', 0.9, 18, 20, %s)",
        (SHA, chat, T0 + timedelta(minutes=8)),
    )
    connection.commit()


def rows(connection: Any, sql: str, *parameters: Any) -> list[tuple[Any, ...]]:
    cursor = connection.cursor()
    cursor.execute(sql, parameters) if parameters else cursor.execute(sql)
    result = [tuple(row) for row in cursor.fetchall()]
    connection.rollback()
    return result


def run(conn: Any, provider: FakeProvider, **options: bool) -> Any:
    return process_chat(conn, provider, SETTINGS, CHAT, **options)


def run_chat(conn: Any, provider: FakeProvider, chat: str, **options: bool) -> Any:
    return process_chat(conn, provider, SETTINGS, chat, **options)


# ------------------------------------------------------------------ digest


def test_the_digest_lists_what_happened_with_numbered_lines(conn: Any) -> None:
    digest = build_digest(conn, CHAT, 80_000)
    text = digest.text
    assert text.splitlines()[0].startswith("CHAT chat-dec; title: Queue work")
    assert "PROMPT Add a queue to the sender" in text
    assert "REPLY I will add a queue." in text
    assert "EDIT src/queue.py (created, 1 edits, +20/-0, code)" in text
    assert "TEST pytest PASSED (2 passed, 0 failed, 0 skipped) `pytest -q`" in text
    assert f"COMMIT {SHA}" in text and "explained 18/20 lines" in text
    # the numbers point at the raw transcript
    first = digest.refs[line_of(text, "PROMPT", "Add a queue")]
    assert (first.raw_object_key, first.raw_line_number, first.kind) == (KEY, 1, "prompt")
    second = digest.refs[line_of(text, "PROMPT", "tabs")]
    assert (second.raw_line_number, second.kind) == (5, "prompt")


def test_the_digest_is_in_time_order(conn: Any) -> None:
    text = build_digest(conn, CHAT, 80_000).text
    order = [line_of(text, label, extra) for label, extra in
             (("PROMPT", "queue"), ("REPLY", "add a queue"), ("EDIT", ""), ("TEST", ""),
              ("PROMPT", "tabs"), ("REPLY", "tabs"))]  # fmt: skip
    assert order == sorted(order)


def test_the_digest_leaves_out_rewound_work(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE event SET is_abandoned = TRUE WHERE session_id = %s AND raw_line_number = 5",
        (CHAT,),
    )
    conn.commit()
    assert "tabs" not in build_digest(conn, CHAT, 80_000).text.replace("Switched to tabs", "")


def test_a_small_limit_shortens_excerpts_then_drops_replies_then_cuts_the_middle(
    conn: Any,
) -> None:
    cursor = conn.cursor()
    for index in range(40):  # a long chat: 40 prompts and 40 replies of 900 characters
        add_event(cursor, 100 + 2 * index, "prompt", "human", 10 + index,
                  {"message": {"content": f"prompt {index} " + "x" * 900}})  # fmt: skip
        add_event(cursor, 101 + 2 * index, "response", "ai", 10 + index,
                  text_message(f"reply {index} " + "y" * 900))  # fmt: skip
    conn.commit()
    full = build_digest(conn, CHAT, 10_000_000)
    assert full.chars > 60_000
    for limit in (40_000, 10_000, 3_000):
        digest = build_digest(conn, CHAT, limit)
        assert digest.chars <= limit
        assert digest.text.splitlines()[0].startswith("CHAT ")
        # every number in the text is in the map, and the map has nothing the text lacks
        numbers = {int(n) for n in re.findall(r"^\[L(\d+)\]", digest.text, re.MULTILINE)}
        assert numbers == set(digest.refs)
    tiny = build_digest(conn, CHAT, 3_000)
    assert "left out" in tiny.text
    assert f"COMMIT {SHA}" in tiny.text  # the footer is always kept
    assert "prompt 0 " in tiny.text and "prompt 39 " in tiny.text  # the start and the end


# ------------------------------------------------------------------ extraction, review, storage


def test_decisions_are_extracted_reviewed_and_stored(conn: Any) -> None:
    provider = FakeProvider()
    result = run(conn, provider)
    assert (result.outcome, result.decisions, result.approved, result.needs_review) == (
        "done",
        2,
        1,
        1,
    )
    assert result.corrections == 1 and result.dropped == 0
    stored = rows(
        conn,
        "SELECT seq, title, status, reviewed_by_kind, reviewer, review_note, model,"
        " prompt_version, commit_shas, evidence_lines, review_history, options"
        " FROM decision WHERE session_id = %s ORDER BY seq",
        CHAT,
    )
    first, second = stored
    assert first[:8] == (
        1,
        "Use a queue",
        "approved",
        "ai",
        "test-reviewer",
        "supported",
        "test-model",
        prompts.PROMPT_VERSION,
    )
    assert first[8] == [SHA]  # the short id was expanded to the linked commit
    assert [item["line"] for item in first[9]] == [1, 3, 4]  # prompt, edit result, test call
    assert {item["key"] for item in first[9]} == {KEY}
    assert first[11] == ["queue", "thread"]
    # The prompt the developer gave at that moment is stored with the decision.
    prompt = rows(
        conn,
        "SELECT prompt_text, prompt_at IS NOT NULL, prompt_uuid IS NOT NULL FROM decision"
        " WHERE session_id = %s AND seq = 1",
        CHAT,
    )[0]
    assert prompt[0] and prompt[1]  # the entry id is set when the transcript has one
    assert (
        prompt[0].strip()
        == rows(
            conn,
            "SELECT COALESCE(payload #>> '{message,content}', '') FROM event"
            " WHERE session_id = %s AND event_type = 'prompt' ORDER BY raw_line_number LIMIT 1",
            CHAT,
        )[0][0].strip()
    )
    assert second[1:4] == ("Indent with tabs", "needs_review", "ai")
    assert second[5] == "result not shown"
    assert [item["line"] for item in second[9]] == [5]  # the invalid number 9999 was dropped
    history = first[10]
    assert len(history) == 1
    assert (history[0]["by_kind"], history[0]["reviewer"], history[0]["status"]) == (
        "ai",
        "test-reviewer",
        "approved",
    )
    assert datetime.fromisoformat(history[0]["at"]).tzinfo is not None


def test_the_review_is_a_separate_call_with_its_own_instructions(conn: Any) -> None:
    provider = FakeProvider()
    run(conn, provider)
    assert [call[0] for call in provider.calls] == [prompts.EXTRACT_SYSTEM, prompts.REVIEW_SYSTEM]
    assert [call[2] for call in provider.calls] == ["test-model", "test-reviewer"]  # two models
    extract_prompt, review_prompt = provider.calls[0][1], provider.calls[1][1]
    assert "Decisions to review" not in extract_prompt
    assert "Decisions to review" in review_prompt and '"index": 0' in review_prompt
    assert "Use a queue" in review_prompt
    assert prompts.EXTRACT_SYSTEM != prompts.REVIEW_SYSTEM
    assert "independent reviewer" in prompts.REVIEW_SYSTEM


def test_developer_corrections_become_correction_rows(conn: Any) -> None:
    run(conn, FakeProvider())
    [row] = rows(
        conn,
        "SELECT k.kind, k.corrected_by, k.detected_by, k.note, e.raw_line_number"
        " FROM correction k JOIN event e ON e.event_id = k.trigger_event_id WHERE k.session_id = %s",
        CHAT,
    )
    assert row == ("developer_correction", "developer", "ai", "use tabs, not spaces", 5)


def test_a_decision_without_a_review_verdict_needs_review(conn: Any) -> None:
    provider = FakeProvider(review={"reviews": [{"index": 0, "verdict": "approve", "note": "ok"}]})
    run(conn, provider)
    statuses = rows(conn, "SELECT status, review_note FROM decision ORDER BY seq")
    assert statuses[0][0] == "approved"
    assert statuses[1][0] == "needs_review" and "no verdict" in statuses[1][1]


def test_invalid_items_are_dropped_and_counted(conn: Any) -> None:
    answer = default_extraction(build_digest(conn, CHAT, 80_000).text)
    answer["decisions"].append({"title": "", "chosen": "x"})
    answer["developer_corrections"].append({"line": 9999, "note": "bad line"})
    result = run(conn, FakeProvider(extract=answer))
    assert (result.decisions, result.corrections, result.dropped) == (2, 1, 2)


def test_a_failing_model_changes_nothing(conn: Any) -> None:
    run(conn, FakeProvider())
    before = rows(conn, "SELECT decision_id, title, status FROM decision ORDER BY seq")
    with pytest.raises(DecisionError, match="extraction failed: the model is down"):
        run(conn, FakeProvider(fail=True), force=True)
    assert rows(conn, "SELECT decision_id, title, status FROM decision ORDER BY seq") == before


@pytest.mark.parametrize("answer", ["not json at all", json.dumps({"decisions": 3})])
def test_an_unusable_answer_is_an_error_and_changes_nothing(conn: Any, answer: str) -> None:
    with pytest.raises(DecisionError, match="extraction failed"):
        run(conn, FakeProvider(extract=answer))
    assert rows(conn, "SELECT count(*) FROM decision") == [(0,)]


def test_a_failing_review_stores_nothing(conn: Any) -> None:
    provider = FakeProvider(review={"reviews": "oops"})
    with pytest.raises(DecisionError, match="review failed"):
        run(conn, provider)
    assert rows(conn, "SELECT count(*) FROM decision") == [(0,)]


def test_no_valid_decision_keeps_what_is_stored(conn: Any) -> None:
    run(conn, FakeProvider())
    result = run(conn, FakeProvider(extract={"decisions": [{"title": ""}]}), force=True)
    assert result.outcome == "no_decisions"
    assert rows(conn, "SELECT count(*) FROM decision") == [(2,)]


# ------------------------------------------------------------------ the re-run policy


def test_up_to_date_decisions_are_not_made_again_unless_forced(conn: Any) -> None:
    run(conn, FakeProvider())
    again = FakeProvider()
    assert run(conn, again).outcome == "up_to_date"
    assert again.calls == []
    assert pending_chats(conn, force=False) == []
    assert pending_chats(conn, force=True) == [CHAT]
    forced = run(conn, again, force=True)
    assert forced.outcome == "done" and len(again.calls) == 2


def test_a_newer_event_makes_the_chat_pending_again(conn: Any) -> None:
    assert pending_chats(conn, force=False) == [CHAT]  # no decisions yet
    run(conn, FakeProvider())
    cursor = conn.cursor()
    add_event(cursor, 7, "prompt", "human", 60 * 24 * 365 * 5, {"message": {"content": "later"}})
    conn.commit()  # an event five years after the decisions were made
    state = chat_state(conn, CHAT)
    assert state is not None and state.needs_decisions()
    assert pending_chats(conn, force=False) == [CHAT]
    assert run(conn, FakeProvider()).outcome == "done"


def test_empty_chats_are_never_processed(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("UPDATE session SET is_empty = TRUE WHERE session_id = %s", (CHAT,))
    conn.commit()
    provider = FakeProvider()
    assert run(conn, provider, force=True).outcome == "empty"
    assert provider.calls == []
    assert pending_chats(conn, force=True) == []


def test_a_chat_without_a_developer_prompt_is_not_processed(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("INSERT INTO session (session_id, is_empty) VALUES ('chat-silent', FALSE)")
    conn.commit()
    provider = FakeProvider()
    result = process_chat(conn, provider, SETTINGS, "chat-silent", force=True)
    assert result.outcome == "empty" and provider.calls == []
    assert "chat-silent" not in pending_chats(conn, force=True)


def test_an_unknown_chat_is_reported(conn: Any) -> None:
    assert process_chat(conn, FakeProvider(), SETTINGS, "nope").outcome == "not_found"


def test_a_dry_run_calls_no_model_and_stores_nothing(conn: Any) -> None:
    provider = FakeProvider()
    result = run(conn, provider, dry_run=True)
    assert result.outcome == "dry_run" and result.digest_chars > 100
    assert provider.calls == []
    assert rows(conn, "SELECT count(*) FROM decision") == [(0,)]
    assert process_chat(conn, None, SETTINGS, CHAT, dry_run=True).outcome == "dry_run"


# ------------------------------------------------------------------ human override


def test_a_person_overrides_a_decision_and_the_history_grows(conn: Any) -> None:
    run(conn, FakeProvider())
    [(second_id,)] = rows(conn, "SELECT decision_id FROM decision WHERE seq = 2")
    set_decision_status(conn, second_id, "approved", "  Dana Lead ", "checked the diff")
    [row] = rows(
        conn,
        "SELECT status, reviewed_by_kind, reviewer, review_note, review_history,"
        " updated_at > created_at FROM decision WHERE decision_id = %s",
        second_id,
    )
    assert row[:4] == ("approved", "human", "Dana Lead", "checked the diff")
    assert [(h["by_kind"], h["reviewer"], h["status"]) for h in row[4]] == [
        ("ai", "test-reviewer", "needs_review"),
        ("human", "Dana Lead", "approved"),
    ]
    assert row[4][1]["note"] == "checked the diff"
    set_decision_status(conn, second_id, "rejected", "Dana Lead")
    [history] = rows(conn, "SELECT review_history FROM decision WHERE decision_id = %s", second_id)
    assert [h["status"] for h in history[0]] == ["needs_review", "approved", "rejected"]


def test_invalid_overrides_are_refused(conn: Any) -> None:
    run(conn, FakeProvider())
    [(decision_id,)] = rows(conn, "SELECT decision_id FROM decision WHERE seq = 1")
    for status, reviewer, note, message in (
        ("draft", "x", None, "status must be"),
        ("approved", "  ", None, "reviewer"),
        ("approved", "x" * 101, None, "reviewer"),
        ("approved", "x", "n" * 1001, "note"),
    ):
        with pytest.raises(DecisionError, match=message):
            set_decision_status(conn, decision_id, status, reviewer, note)
    with pytest.raises(DecisionError, match="no decision 999999"):
        set_decision_status(conn, 999_999, "approved", "x")
    assert rows(conn, "SELECT reviewed_by_kind FROM decision WHERE seq = 1") == [("ai",)]


def test_a_new_run_keeps_human_reviewed_decisions_and_replaces_the_rest(conn: Any) -> None:
    run(conn, FakeProvider())
    [(first_id,)] = rows(conn, "SELECT decision_id FROM decision WHERE seq = 1")
    set_decision_status(conn, first_id, "rejected", "Dana", "wrong")
    run(conn, FakeProvider(), force=True)
    stored = rows(
        conn, "SELECT decision_id, seq, status, reviewed_by_kind FROM decision ORDER BY seq"
    )
    assert stored[0] == (first_id, 1, "rejected", "human")  # untouched
    assert len(stored) == 3  # the AI's old second decision was replaced by two new ones
    assert [row[1] for row in stored] == [1, 2, 3]  # numbered after the kept one
    assert [row[3] for row in stored[1:]] == ["ai", "ai"]
    [history] = rows(conn, "SELECT review_history FROM decision WHERE decision_id = %s", first_id)
    assert len(history[0]) == 2  # the AI entry and the human entry, nothing lost


def test_developer_corrections_are_replaced_not_duplicated(conn: Any) -> None:
    run(conn, FakeProvider())
    run(conn, FakeProvider(), force=True)
    assert rows(conn, "SELECT count(*) FROM correction WHERE kind = 'developer_correction'") == [
        (1,)
    ]


def test_corrections_found_by_rules_are_not_touched(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO correction (session_id, kind, corrected_by, trigger_event_id, detected_by)"
        " SELECT session_id, 'rework', 'claude', event_id, 'rule' FROM event"
        " WHERE raw_line_number = 2"
    )
    conn.commit()
    run(conn, FakeProvider())
    run(conn, FakeProvider(), force=True)
    assert rows(conn, "SELECT kind, detected_by FROM correction ORDER BY kind") == [
        ("developer_correction", "ai"),
        ("rework", "rule"),
    ]


# ------------------------------------------------------------------ the command


@pytest.fixture
def cli_env(database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch) -> DatabaseSettings:
    monkeypatch.setenv("POSTGRES_HOST", database.host)
    monkeypatch.setenv("POSTGRES_PORT", str(database.port))
    monkeypatch.setenv("POSTGRES_DB", database.name)
    monkeypatch.setenv("POSTGRES_USER", database.user)
    monkeypatch.setenv("POSTGRES_PASSWORD", database.password.reveal())
    for name in ("CCP_DECISION_MODEL", "CCP_DECISION_MAX_CHARS", "CCP_DECISION_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CCP_DECISION_MODEL", "test-model")
    monkeypatch.setenv("CCP_REVIEW_MODEL", "test-reviewer")
    return database


def test_the_command_runs_lists_and_reviews(
    conn: Any, cli_env: DatabaseSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = FakeProvider()
    assert decisions_main(["run", "--chat", "chat-decisions"], provider) == 0
    out = capsys.readouterr().out
    assert "2 decision(s): 1 approved by AI, 1 need review; 1 developer correction(s)" in out
    assert "Add a queue" not in out  # counts only, never the chat's text

    assert decisions_main(["list", "--chat", CHAT]) == 0
    listing = capsys.readouterr().out
    assert "[approved] Use a queue" in listing and "reviewed by ai: test-reviewer" in listing
    [(decision_id,)] = rows(conn, "SELECT decision_id FROM decision WHERE seq = 2")

    assert (
        decisions_main(["review", str(decision_id), "approve", "--by", "Dana", "--note", "ok"]) == 0
    )
    assert "now approved (by Dana)" in capsys.readouterr().out
    decisions_main(["list", "--chat", CHAT])
    assert "reviewed by human: Dana" in capsys.readouterr().out

    assert decisions_main(["run", "--pending"], provider) == 0
    assert "No chat needs decisions." in capsys.readouterr().out
    assert decisions_main(["run", "--chat", CHAT], provider) == 0
    assert "up to date" in capsys.readouterr().out


def test_the_command_dry_run_and_errors(
    conn: Any, cli_env: DatabaseSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert decisions_main(["run", "--pending", "--dry-run"]) == 0
    assert "chat-dec  digest" in capsys.readouterr().out
    assert rows(conn, "SELECT count(*) FROM decision") == [(0,)]

    assert decisions_main(["run", "--chat", "zzz"]) == 1
    assert "no chat 'zzz'" in capsys.readouterr().err
    assert decisions_main(["review", "424242", "reject", "--by", "Dana"]) == 1
    assert "no decision 424242" in capsys.readouterr().err

    assert decisions_main(["run", "--chat", CHAT], FakeProvider(fail=True)) == 1
    assert "failed: extraction failed" in capsys.readouterr().err


def test_the_command_refuses_an_ambiguous_chat_id(
    conn: Any, cli_env: DatabaseSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_chat(conn, "chat-decisions-2")
    assert decisions_main(["list", "--chat", "chat-decisions"]) == 1
    assert "matches 2 chats" in capsys.readouterr().err
    assert decisions_main(["list", "--chat", "chat-decisions-2"]) == 0


def test_a_chat_whose_session_ended_after_its_last_message_is_finished(conn: Any) -> None:
    """The sweep processes a chat when its session ended (no need to wait for quiet time)."""
    from ccp.decisions.plugin import session_ended

    assert session_ended(conn, CHAT) is False
    cursor = conn.cursor()
    cursor.execute("SELECT max(occurred_at) FROM event WHERE session_id = %s", (CHAT,))
    (last,) = cursor.fetchone()
    cursor.execute(
        "INSERT INTO chat_session (session_id, seq, start_source, started_at, ended_at, "
        "end_reason, run_key) VALUES (%s, 1, 'startup', %s, %s, 'other', 'run-1')",
        (CHAT, last - timedelta(hours=1), last + timedelta(minutes=5)),
    )
    conn.commit()
    assert session_ended(conn, CHAT) is True
    # A session that ended long BEFORE the last message does not count: the chat went on.
    cursor.execute(
        "UPDATE chat_session SET ended_at = %s WHERE run_key = 'run-1'",
        (last - timedelta(hours=2),),
    )
    conn.commit()
    assert session_ended(conn, CHAT) is False


# ----------------------------------------------------------- who decided, files, shell changes


def test_a_decision_stores_who_decided_and_the_files_it_changed(conn: Any) -> None:
    process_chat(conn, FakeProvider(), SETTINGS, CHAT)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT decided_by, files FROM decision WHERE session_id = %s ORDER BY seq", (CHAT,)
    )
    (first, second) = cursor.fetchall()
    assert first[0] == "claude" and first[1] == [
        {"path": "src/queue.py", "change": "created", "via": "edit"}
    ]
    assert second[0] == "developer" and second[1] == []  # an unknown line number names no file


def test_files_changed_by_a_shell_command_are_in_the_digest_and_the_decision(conn: Any) -> None:
    cursor = conn.cursor()
    command = (
        "cd /w && cat > Uploads.cs <<'EOF'\nclass A {}\nEOF\nrm -f old.http && dotnet new webapi"
    )
    add_event(
        cursor, 30, "tool_call", "ai", 40,
        {"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}},
    )  # fmt: skip
    conn.commit()
    digest = build_digest(conn, CHAT, 80_000)
    shell = line_of(digest.text, "SHELL")
    assert (
        "created Uploads.cs; deleted old.http; created (project files from dotnet new webapi)"
        in (digest.text)
    )
    answer = default_extraction(digest.text)
    answer["decisions"][0]["file_lines"] = [line_of(digest.text, "EDIT"), shell]
    process_chat(conn, FakeProvider(extract=answer), SETTINGS, CHAT)
    cursor.execute("SELECT files FROM decision WHERE session_id = %s AND seq = 1", (CHAT,))
    paths = [(f["path"], f["via"]) for f in cursor.fetchone()[0]]
    assert paths == [
        ("src/queue.py", "edit"),
        ("Uploads.cs", "shell"),
        ("old.http", "shell"),
        ("(project files from dotnet new webapi)", "shell"),
    ]


def test_thinking_notes_are_listed_cited_and_copied_from_the_data(conn: Any) -> None:
    cursor = conn.cursor()
    think = add_event(cursor, 40, "thinking", "ai", 50, {})
    cursor.execute(
        "INSERT INTO thinking (event_id, block_index, text) VALUES (%s, 0, %s)",
        (think, "I will use a queue because it is simple to test."),
    )
    empty = add_event(cursor, 41, "thinking", "ai", 51, {})
    cursor.execute(
        "INSERT INTO thinking (event_id, block_index, text) VALUES (%s, 0, '')", (empty,)
    )
    conn.commit()
    digest = build_digest(conn, CHAT, 80_000)
    note = line_of(digest.text, "THINK")
    assert "I will use a queue because it is simple to test." in digest.text
    assert digest.text.count("THINK") == 1  # the block without text is not listed

    answer = default_extraction(digest.text)
    answer["decisions"][0]["thinking_lines"] = [note, 9999, line_of(digest.text, "PROMPT")]
    process_chat(conn, FakeProvider(extract=answer), SETTINGS, CHAT)
    cursor.execute("SELECT thinking FROM decision WHERE session_id = %s AND seq = 1", (CHAT,))
    (stored,) = cursor.fetchone()
    # Only the THINK line counts; a made-up or non-thinking line number adds nothing.
    assert [t["text"] for t in stored] == ["I will use a queue because it is simple to test."]
    assert stored[0]["key"].startswith("sessions/") and stored[0]["line"] == 40


def test_the_usage_of_both_calls_is_stored_with_the_decisions(conn: Any) -> None:
    from ccp.decisions.provider import CallUsage

    class Metered(FakeProvider):
        last_usage: CallUsage | None = None

        def complete(self, system: str, prompt: str, model: str) -> str:
            answer = super().complete(system, prompt, model)
            wrote = system == prompts.EXTRACT_SYSTEM
            self.last_usage = CallUsage(
                model, 1000 if wrote else 2000, 300 if wrote else 50, cost_usd=0.5
            )
            return answer

    process_chat(conn, Metered(), SETTINGS, CHAT)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT llm_usage FROM decision WHERE session_id = %s", (CHAT,))
    (usage,) = [row[0] for row in cursor.fetchall()]
    assert usage["write"]["input_tokens"] == 1000 and usage["write"]["model"] == "test-model"
    assert usage["review"]["output_tokens"] == 50 and usage["review"]["model"] == "test-reviewer"


def test_a_chat_without_decisions_is_not_asked_again_until_it_has_new_activity(conn: Any) -> None:
    from ccp.decisions.repository import pending_chats

    nothing = FakeProvider(extract={"decisions": [], "developer_corrections": []})
    result = process_chat(conn, nothing, SETTINGS, CHAT)
    assert result.outcome == "no_decisions"
    assert CHAT not in pending_chats(conn, False)  # checked: no second call at the next sweep
    assert process_chat(conn, nothing, SETTINGS, CHAT).outcome == "up_to_date"
    assert len(nothing.calls) == 1

    cursor = conn.cursor()
    add_event(cursor, 60, "prompt", "human", 90, {"message": {"content": "one more thing"}})
    cursor.execute(
        "UPDATE event SET occurred_at = now() + interval '1 hour' WHERE raw_line_number = 60"
    )
    conn.commit()
    assert CHAT in pending_chats(conn, False)  # new activity: it is pending again


ADVICE = "Usually it isn't a good design for a plain synchronous upload."


def seed_advice(connection: Any, chat: str = CHAT) -> None:
    """A question, advice with a question back, then a question and a rejected plan."""
    cursor = connection.cursor()
    add = partial(add_event, cursor, chat=chat)
    add(20, "prompt", "human", 20, {"message": {"content": "is a 200 MB upload a good design?"}})
    cursor.execute(
        "UPDATE event SET entry_uuid = 'u-ask' WHERE session_id = %s AND raw_line_number = 20",
        (chat,),
    )
    add(21, "response", "ai", 21, text_message(f"{ADVICE} Where do the files come from?"))
    ask = {"questions": [{"question": "Where from?", "options": [
        {"label": "Browser (Recommended)", "description": "direct upload"}, {"label": "Partner"}]}]}  # fmt: skip
    add(22, "tool_call", "ai", 22, {"message": {"content": [
        {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": ask}]}})  # fmt: skip
    add(23, "tool_result", "system", 23, {"message": {"content": [
        {"type": "tool_result", "tool_use_id": "q1", "content": "Answer: Partner"}]}})  # fmt: skip
    add(24, "tool_call", "ai", 24, {"message": {"content": [
        {"type": "tool_use", "id": "p1", "name": "ExitPlanMode", "input": {"plan": "Use a SAS URL"}}]}})  # fmt: skip
    add(25, "tool_result", "system", 25, {"message": {"content": [
        {"type": "tool_result", "tool_use_id": "p1", "is_error": True, "content": "no"}]}})  # fmt: skip
    connection.commit()


def advice_answer(prompt: str) -> dict[str, Any]:
    ask = line_of(prompt, "PROMPT", "200 MB")
    reply = line_of(prompt, "REPLY", "plain synchronous")
    plan = line_of(prompt, "PLAN")
    edit = line_of(prompt, "EDIT")
    return {"decisions": [
        {"title": "Large upload design", "kind": "advice", "raised_by": "developer asked",
         "goal": "judge a 200 MB upload", "short_answer": "Not recommended at that size",
         "chosen": "No single choice yet", "options": ["background job: removes long requests"],
         "not_chosen": [{"option": "single request", "why": "proxy limits"}],
         "use_when": ["under 50 MB -> current endpoint"], "waiting_for": "where the files come from",
         "outcome": "waiting", "decided_by": "claude", "evidence_lines": [ask, reply],
         "quotes": [{"line": reply, "text": ADVICE}, {"line": reply, "text": "words nobody said here"}]},
        {"title": "Azure upload plan", "kind": "plan", "chosen": "SAS URL plan", "outcome": "done",
         "decided_by": "claude", "evidence_lines": [plan]},
        {"title": "Queue in the sender", "kind": "code change", "chosen": "queue",
         "decided_by": "developer", "file_lines": [edit], "evidence_lines": [edit],
         "follows_decision": 1, "follows_how": "followed"},
    ]}  # fmt: skip


def stored(connection: Any, chat: str = CHAT) -> dict[str, tuple[Any, ...]]:
    found = rows(
        connection,
        "SELECT title, kind, raised_by, outcome, details, prompt_uuid, follows_prompt_uuid,"
        " follows_session_id FROM decision WHERE session_id = %s ORDER BY seq",
        chat,
    )
    return {row[0]: row[1:] for row in found}


def test_ask_and_plan_lines_are_in_the_digest_with_the_developers_answer(conn: Any) -> None:
    seed_advice(conn)
    text = build_digest(conn, CHAT, 80_000).text
    assert 'ASK Claude asked the developer "Where from?" options: Browser (Recommended)' in text
    assert "-> answer: Answer: Partner" in text
    assert "PLAN Claude put this plan to the developer: Use a SAS URL -> REJECTED" in text
    assert "Usually it isn't a good design for a plain synchronous upload. Where do" in text


def test_advice_is_stored_with_exact_words_and_takes_the_outcome_of_its_follower(conn: Any) -> None:
    seed_advice(conn)
    provider = FakeProvider(extract=advice_answer(build_digest(conn, CHAT, 80_000).text))
    assert run(conn, provider).outcome == "done"
    decisions = stored(conn)
    advice, plan, code = (decisions[t] for t in ("Large upload design", "Azure upload plan",
                                                 "Queue in the sender"))  # fmt: skip
    assert advice[:3] == ("advice", "developer asked", "followed")  # the later decision followed it
    assert advice[3]["short_answer"] == "Not recommended at that size"
    assert advice[3]["use_when"] == ["under 50 MB -> current endpoint"]
    assert advice[3]["not_chosen"] == [{"option": "single request", "why": "proxy limits"}]
    assert [q["text"] for q in advice[3]["quotes"]] == [ADVICE]  # the invented words are dropped
    assert advice[3]["quotes"][0]["who"] == "claude" and advice[4] == "u-ask"
    assert plan[:3] == ("plan", None, "not followed")  # the developer rejected the plan
    assert code[:3] == ("code change", None, "done")
    assert (code[5], code[6]) == ("u-ask", CHAT)  # linked to the advice by the prompt it answered


def test_advice_of_another_chat_is_offered_and_its_outcome_follows_the_link(conn: Any) -> None:
    other = "chat-other"
    seed_chat(conn, other)
    seed_advice(conn, other)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO project (project_key, key_kind, name) VALUES ('p', 'folder', 'p')"
                   " RETURNING project_id")  # fmt: skip
    project = cursor.fetchone()[0]
    cursor.execute("UPDATE session SET project_id = %s", (project,))
    conn.commit()
    first = advice_answer(build_digest(conn, other, 80_000).text)
    first["decisions"] = first["decisions"][:1]
    assert run_chat(conn, FakeProvider(extract=first), other).outcome == "done"
    assert stored(conn, other)["Large upload design"][2] == "waiting"

    digest = build_digest(conn, CHAT, 80_000)
    assert "[A1] OPEN ADVICE (advice) Large upload design" in digest.text
    assert "waiting for: where the files come from" in digest.text
    edit = line_of(digest.text, "EDIT")
    answer = {"decisions": [{"title": "Other approach", "chosen": "thread", "decided_by": "developer",
                             "file_lines": [edit], "evidence_lines": [edit],
                             "follows_open_advice": 1, "follows_how": "not followed"}]}  # fmt: skip
    assert run(conn, FakeProvider(extract=answer)).outcome == "done"
    assert stored(conn, other)["Large upload design"][2] == "not followed"
    mine = stored(conn)["Other approach"]
    assert (mine[5], mine[6]) == ("u-ask", other)

    # The chat is written again without that link: the advice is waiting again.
    plain = {
        "decisions": [{"title": "Other approach", "chosen": "thread", "evidence_lines": [edit]}]
    }
    assert run(conn, FakeProvider(extract=plain), force=True).outcome == "done"
    assert stored(conn, other)["Large upload design"][2] == "waiting"


def test_a_link_to_advice_that_does_not_exist_is_ignored(conn: Any) -> None:
    edit = line_of(build_digest(conn, CHAT, 80_000).text, "EDIT")
    answer = {"decisions": [{"title": "Queue", "chosen": "queue", "file_lines": [edit],
                             "evidence_lines": [edit], "follows_decision": 5,
                             "follows_open_advice": 3, "follows_how": "followed"}]}  # fmt: skip
    assert run(conn, FakeProvider(extract=answer)).outcome == "done"
    row = rows(conn, "SELECT follows_prompt_uuid, details ? 'follows_how' FROM decision")[0]
    assert row == (None, False)


def test_a_new_prompt_version_alone_does_not_rewrite_an_old_chat(conn: Any) -> None:
    assert run(conn, FakeProvider()).outcome == "done"
    cursor = conn.cursor()
    cursor.execute("UPDATE session SET decisions_checked_version = '5'")
    conn.commit()
    assert chat_state(conn, CHAT).outdated  # type: ignore[union-attr]
    assert CHAT not in pending_chats(conn, False)  # no tokens are spent on it
    add_event(cursor, 70, "prompt", "human", 90, {"message": {"content": "one more thing"}})
    cursor.execute(
        "UPDATE event SET occurred_at = now() + interval '1 hour' WHERE raw_line_number = 70"
    )
    conn.commit()
    assert CHAT in pending_chats(conn, False)  # new activity: rewritten with the new prompt


class PerPartProvider(FakeProvider):
    """Writes one decision for the first developer message of whatever part it is shown."""

    def complete(self, system: str, prompt: str, model: str) -> str:
        if system == prompts.EXTRACT_SYSTEM:
            self.calls.append((system, prompt, model))
            first = line_of(prompt, "PROMPT")
            part = len([c for c in self.calls if c[0] == prompts.EXTRACT_SYSTEM])
            return json.dumps({"decisions": [{
                "title": f"Part {part}", "chosen": f"choice {part}", "evidence_lines": [first],
                "follows_decision": 1 if part == 2 else None}]})  # fmt: skip
        return super().complete(system, prompt, model)


def test_a_long_chat_is_read_in_parts_and_every_part_gives_its_decisions(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ccp.decisions.runner.WINDOW_CHARS", 200
    )  # a part for each developer message
    provider = PerPartProvider(
        review={"reviews": [{"index": 0, "verdict": "approve", "note": "ok"}]}
    )
    result = run(conn, provider)
    assert result.outcome == "done" and result.decisions == 2
    assert len([c for c in provider.calls if c[0] == prompts.EXTRACT_SYSTEM]) == 2
    assert len([c for c in provider.calls if c[0] == prompts.REVIEW_SYSTEM]) == 2
    assert [r[0] for r in rows(conn, "SELECT title FROM decision ORDER BY seq")] == [
        "Part 1",
        "Part 2",
    ]
    assert all(r[0] == "approved" for r in rows(conn, "SELECT status FROM decision"))
    first, second = (c[1] for c in provider.calls if c[0] == prompts.EXTRACT_SYSTEM)
    assert "Add a queue" in first and "Use tabs" not in first  # a part is only its own messages
    assert "Use tabs" in second and "Add a queue" not in second


def test_every_model_that_did_the_work_is_recorded_with_its_effort_level(conn: Any) -> None:
    cursor = conn.cursor()
    cursor.execute("UPDATE event SET entry_uuid = 'u-first' WHERE raw_line_number = 1")
    for line, model, effort in ((2, "claude-opus-5-5", "high"), (6, "claude-sonnet-5-5", "medium")):
        cursor.execute(
            "INSERT INTO model_usage (session_id, message_id, event_id, model, effort)"
            " SELECT session_id, 'm' || raw_line_number, event_id, %s, %s FROM event"
            " WHERE session_id = %s AND raw_line_number = %s",
            (model, effort, CHAT, line),
        )
    conn.commit()
    assert run(conn, FakeProvider()).outcome == "done"
    used = rows(conn, "SELECT seq, details -> 'models_used' FROM decision ORDER BY seq")
    assert used[0][1] == [{"model": "claude-opus-5-5", "effort": "high", "responses": 1}]
    # the second decision's prompt has no recorded entry uuid here, so nothing is claimed for it
    assert used[1][1] == []
