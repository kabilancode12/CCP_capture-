"""The explorer website on an invented chat: login, roles, access log, review flow, CSRF, admin.

Runs the real Starlette application against a throwaway database (all migrations applied) through
Starlette's TestClient. The rows are inserted directly, so the test does not depend on the indexer.
"""

import json
import re
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from ccp.db.connection import connect
from ccp.explorer.app import create_app
from ccp.explorer.users import account_id_for_email, create_user
from ccp.settings import DatabaseSettings

pytestmark = pytest.mark.integration

SECRET = "invented-explorer-secret-for-tests"  # noqa: S105
PASSWORD = "an invented password"  # noqa: S105
XSS = "<script>alert('x')</script>"
CHAT_A, CHAT_B, CHAT_EMPTY = "chat-a", "chat-b", "chat-empty"
KEY_A = f"sessions/{CHAT_A}/main/000000000000.jsonl"
KEY_B = f"sessions/{CHAT_B}/main/000000000000.jsonl"


def _sql(database: DatabaseSettings, sql: str, *parameters: Any) -> list[tuple[Any, ...]]:
    connection = connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql, parameters) if parameters else cursor.execute(sql)
        rows = [tuple(r) for r in cursor.fetchall()] if cursor.description else []
        connection.commit()
        return rows
    finally:
        connection.close()


def _event(
    database: DatabaseSettings, session: str, key: str, line: int, kind: str, actor: str,
    minute: int, content: Any, **extra: Any,
) -> int:  # fmt: skip
    payload = {"message": {"content": content}, **extra}
    return int(
        _sql(
            database,
            "INSERT INTO event (session_id, event_type, actor, occurred_at, payload, raw_object_key,"
            " raw_line_number) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING event_id",
            session, kind, actor, f"2026-11-01T10:{minute:02d}:00Z", json.dumps(payload), key, line,
        )[0][0]
    )  # fmt: skip


@pytest.fixture
def site(database: DatabaseSettings) -> dict[str, Any]:
    """Two accounts, one project, three chats and six users; returns the ids."""
    connection = connect(database)
    try:
        ana = account_id_for_email(connection, "ana@example.com")
        ben = account_id_for_email(connection, "ben@example.com")
        for email, role, account in [
            ("ana.dev@example.com", "developer", ana),
            ("ben.dev@example.com", "developer", ben),
            ("lead@example.com", "lead", None),
            ("auditor@example.com", "auditor", None),
            ("admin@example.com", "admin", None),
        ]:
            create_user(
                connection, email, role, PASSWORD, full_name=role.title(), account_id=account
            )
        create_user(connection, "off@example.com", "lead", PASSWORD)
        connection.cursor().execute(
            "UPDATE ui_user SET disabled = TRUE WHERE email = 'off@example.com'"
        )
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO project (project_key, key_kind, name, folders, label) "
            "VALUES ('github.com/acme/billing', 'git_remote', 'billing', ARRAY['/work/billing'], NULL) "
            "RETURNING project_id"
        )
        project = int(cursor.fetchone()[0])
        for chat, account, title, empty in [
            (CHAT_A, ana, "Fix the login bug", False),
            (CHAT_B, ben, "Secret plans of Ben", False),
            (CHAT_EMPTY, ana, None, True),
        ]:
            cursor.execute(
                "INSERT INTO session (session_id, account_id, project_id, title, is_empty, started_at,"
                " last_activity_at) VALUES (%s, %s, %s, %s, %s, '2026-11-01T10:00:00Z',"
                " '2026-11-01T11:00:00Z')",
                (chat, account, project, title, empty),
            )
        connection.commit()
    finally:
        connection.close()
    _seed_chat_a(database)
    _event(database, CHAT_B, KEY_B, 1, "prompt", "human", 1, "Ben private prompt text")
    _sql(
        database,
        "INSERT INTO decision (session_id, seq, title, status) VALUES (%s, 1, 'Ben decision', 'draft')",
        CHAT_B,
    )
    return {"ana": ana, "ben": ben, "project": project}


def _seed_chat_a(database: DatabaseSettings) -> None:
    """The content of chat A: messages, a failing test run, an edit, a correction, decisions ..."""
    _event(database, CHAT_A, KEY_A, 1, "prompt", "human", 1, f"Please fix the login. {XSS}")
    call = _event(database, CHAT_A, KEY_A, 2, "tool_call", "ai", 2,
                  [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}])  # fmt: skip
    result = _event(database, CHAT_A, KEY_A, 3, "tool_result", "system", 3,
                    [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "SECRET OUTPUT"}])  # fmt: skip
    reply = _event(
        database,
        CHAT_A,
        KEY_A,
        4,
        "response",
        "ai",
        4,
        [{"type": "text", "text": "I fixed the bug."}],
    )
    think = _event(
        database, CHAT_A, KEY_A, 5, "thinking", "ai", 4, [{"type": "thinking", "thinking": "hmm"}]
    )
    edit = _event(database, CHAT_A, KEY_A, 6, "tool_result", "system", 5, [{"type": "tool_result", "tool_use_id": "e1", "content": "ok"}])  # fmt: skip
    _event(database, CHAT_A, KEY_A, 7, "slash_command", "human", 6,
           "<command-name>/model</command-name><command-args>opus</command-args>")  # fmt: skip
    _event(database, CHAT_A, KEY_A, 8, "prompt", "human", 7, "Rewound idea")
    _sql(
        database,
        "UPDATE event SET is_abandoned = TRUE WHERE raw_line_number = 8 AND session_id = %s",
        CHAT_A,
    )
    sub_key = f"sessions/{CHAT_A}/agent-a1/000000000000.jsonl"
    _event(
        database,
        CHAT_A,
        sub_key,
        1,
        "response",
        "subagent",
        8,
        [{"type": "text", "text": "sub-agent text"}],
    )
    _sql(
        database,
        "INSERT INTO thinking (event_id, block_index, text) VALUES (%s, 0, 'Let me think about it')",
        think,
    )
    run = int(_sql(
        database,
        "INSERT INTO test_run (session_id, call_event_id, result_event_id, tool_use_id, command, framework,"
        " passed, exit_code, tests_passed, tests_failed, tests_skipped, output_source)"
        " VALUES (%s, %s, %s, 't1', 'pytest -q', 'pytest', FALSE, 1, 3, 1, 0, 'inline') RETURNING test_run_id",
        CHAT_A, call, result,
    )[0][0])  # fmt: skip
    _sql(database, "INSERT INTO test_case (test_run_id, position, name, outcome, message) VALUES (%s, 1, 'test_login', 'failed', 'assert 1 == 2')", run)  # fmt: skip
    _sql(database, "INSERT INTO file_change (session_id, event_id, path, absolute_path, change_type, lines_added, file_role, tests_added) VALUES (%s, %s, 'src/login.py', '/work/src/login.py', 'modified', 4, 'code', 0)", CHAT_A, edit)  # fmt: skip
    _sql(database, "INSERT INTO correction (session_id, kind, corrected_by, trigger_event_id, fix_event_id, detected_by, note) VALUES (%s, 'test_fix', 'test', %s, %s, 'rule', 'fixed after a failing run')", CHAT_A, result, reply)  # fmt: skip
    _sql(database, "INSERT INTO chat_session (session_id, seq, start_source, started_at, end_reason) VALUES (%s, 1, 'startup', '2026-11-01T10:00:00Z', 'inferred')", CHAT_A)  # fmt: skip
    _sql(database, "INSERT INTO agent_run (session_id, agent_id, agent_type, description, model, effort, workflow_run_id, workflow_name, event_count) VALUES (%s, 'a1', 'Explore', 'Look around', 'model-a', 'high', 'run1', 'review-flow', 1)", CHAT_A)  # fmt: skip
    _sql(database, "INSERT INTO model_usage (session_id, message_id, event_id, model, input_tokens, output_tokens, effort, requested_model) VALUES (%s, 'm1', %s, 'model-a', 100, 50, 'xhigh', 'model-a')", CHAT_A, reply)  # fmt: skip
    _sql(database, "INSERT INTO decision (session_id, seq, title, goal, options, chosen, reason, status, reviewed_by_kind, reviewer, model, evidence_lines, commit_shas) VALUES (%s, 1, 'Use bcrypt', 'Hash passwords', '[\"bcrypt\", \"md5\"]'::jsonb, 'bcrypt', 'it is slow', 'approved', 'ai', 'claude-opus-5-5', 'claude-opus-5-5', %s::jsonb, ARRAY['abcdef1234567'])", CHAT_A, json.dumps([{"key": KEY_A, "line": 2}]))  # fmt: skip
    _sql(database, "INSERT INTO decision (session_id, seq, title, status, reviewed_by_kind, reviewer) VALUES (%s, 2, 'Skip the cache', 'needs_review', 'ai', 'claude-opus-5-5')", CHAT_A)  # fmt: skip


@pytest.fixture
def app(database: DatabaseSettings, site: dict[str, Any]) -> Starlette:
    return create_app(database, secret=SECRET)


class Browser:
    """A TestClient with login and form helpers."""

    def __init__(self, app: Starlette, prefix: str = "") -> None:
        self.prefix = prefix
        self.client = TestClient(app, follow_redirects=False)

    def get(self, path: str) -> Any:
        return self.client.get(self.prefix + path)

    def token(self, page: str) -> str:
        match = re.search(r'name="csrf" value="([^"]+)"', page)
        assert match, "no csrf token on the page"
        return match.group(1)

    def post(self, path: str, page: str, **fields: str) -> Any:
        """Post a form with the CSRF token found on ``page``."""
        return self.client.post(self.prefix + path, data={"csrf": self.token(page), **fields})

    def login(self, email: str, password: str = PASSWORD) -> Any:
        page = self.get("/login").text
        return self.post("/login", page, email=email, password=password)


def _as(app: Starlette, email: str) -> Browser:
    browser = Browser(app)
    assert browser.login(email).status_code == 303
    return browser


def _log(database: DatabaseSettings, action: str) -> list[tuple[Any, ...]]:
    return _sql(
        database,
        "SELECT u.email, l.target FROM ui_access_log l JOIN ui_user u USING (user_id)"
        " WHERE l.action = %s ORDER BY l.log_id",
        action,
    )


# ---------------------------------------------------------------- login and CSRF


def test_anonymous_visitors_are_sent_to_the_login_page(app: Starlette) -> None:
    browser = Browser(app)
    for path in ("/", "/project/1", f"/chat/{CHAT_A}", "/admin/users", "/access-log"):
        response = browser.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_login_checks_the_password_and_the_disabled_flag(app: Starlette) -> None:
    browser = Browser(app)
    assert browser.login("ana.dev@example.com", "a wrong password!").status_code == 401
    assert browser.login("nobody@example.com").status_code == 401
    assert browser.login("off@example.com").status_code == 401
    ok = browser.login("Ana.Dev@Example.com")
    assert ok.status_code == 303 and "httponly" in ok.headers["set-cookie"].lower()
    assert browser.get("/").status_code == 200


def test_repeated_failures_are_slowed_down(app: Starlette) -> None:
    browser = Browser(app)
    for _ in range(5):
        assert browser.login("lead@example.com", "wrong password here").status_code == 401
    assert browser.login("lead@example.com").status_code == 429


def test_a_form_without_a_valid_csrf_token_is_rejected(
    app: Starlette, database: DatabaseSettings
) -> None:
    anonymous = Browser(app)
    anonymous.get("/login")
    bad = anonymous.client.post(
        "/login", data={"email": "lead@example.com", "password": PASSWORD, "csrf": "forged"}
    )
    assert bad.status_code == 403
    assert (
        anonymous.client.post(
            "/login", data={"email": "lead@example.com", "password": PASSWORD}
        ).status_code
        == 403
    )

    lead = _as(app, "lead@example.com")
    page = lead.get(f"/chat/{CHAT_A}?tab=decisions").text
    decision = _sql(
        database, "SELECT decision_id FROM decision WHERE seq = 2 AND session_id = %s", CHAT_A
    )[0][0]
    path = f"/chat/{CHAT_A}/decisions/{decision}/review"
    assert lead.client.post(path, data={"status": "approve"}).status_code == 403
    assert lead.client.post(path, data={"status": "approve", "csrf": "forged"}).status_code == 403
    assert lead.client.post("/logout", data={}).status_code == 403
    assert (
        _sql(database, "SELECT status FROM decision WHERE decision_id = %s", decision)[0][0]
        == "needs_review"
    )
    assert lead.post(path, page, status="approve").status_code == 303


def test_a_csrf_token_from_another_session_is_rejected(app: Starlette) -> None:
    other = _as(app, "admin@example.com")
    token_page = other.get("/admin/users").text
    lead = _as(app, "lead@example.com")
    response = lead.client.post("/logout", data={"csrf": other.token(token_page)})
    assert response.status_code == 403


def test_logout_ends_the_session(app: Starlette) -> None:
    lead = _as(app, "lead@example.com")
    page = lead.get("/").text
    assert lead.post("/logout", page).status_code == 303
    assert lead.get("/").status_code == 303


def test_pages_carry_security_headers(app: Starlette) -> None:
    response = Browser(app).get("/login")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert Browser(app).get("/style.css").headers["content-type"].startswith("text/css")


# ---------------------------------------------------------------- roles and the access log


def test_a_developer_opens_only_chats_of_their_own_account(
    app: Starlette, database: DatabaseSettings
) -> None:
    ana = _as(app, "ana.dev@example.com")
    own = ana.get(f"/chat/{CHAT_A}")
    assert own.status_code == 200 and "Please fix the login" in own.text
    assert ana.get(f"/chat/{CHAT_B}").status_code == 404
    assert ana.get("/chat/no-such-chat").status_code == 404
    assert ana.get(f"/chat/{CHAT_B}?tab=decisions").status_code == 404
    assert _log(database, "view_chat") == [("ana.dev@example.com", CHAT_A)]


def test_a_developer_sees_only_counts_of_other_accounts_chats(
    app: Starlette, site: dict[str, Any]
) -> None:
    ana = _as(app, "ana.dev@example.com")
    page = ana.get(f"/project/{site['project']}").text
    assert "Fix the login bug" in page
    assert "Secret plans of Ben" not in page and CHAT_B not in page
    assert "(content hidden)" in page
    assert "ben@example.com" not in page and "ana@example.com" not in page
    assert "account-" in page


def test_a_developer_without_a_linked_account_opens_nothing(
    app: Starlette, database: DatabaseSettings
) -> None:
    connection = connect(database)
    create_user(connection, "free@example.com", "developer", PASSWORD)
    connection.commit()
    connection.close()
    free = _as(app, "free@example.com")
    assert free.get(f"/chat/{CHAT_A}").status_code == 404
    assert free.get(f"/chat/{CHAT_B}").status_code == 404


@pytest.mark.parametrize("email", ["lead@example.com", "auditor@example.com", "admin@example.com"])
def test_leads_auditors_and_admins_open_every_chat(app: Starlette, email: str) -> None:
    browser = _as(app, email)
    assert "Ben private prompt text" in browser.get(f"/chat/{CHAT_B}").text
    assert "Please fix the login" in browser.get(f"/chat/{CHAT_A}").text


def test_every_chat_view_is_written_to_the_access_log(
    app: Starlette, database: DatabaseSettings
) -> None:
    lead = _as(app, "lead@example.com")
    lead.get(f"/chat/{CHAT_A}")
    lead.get(f"/chat/{CHAT_A}?tab=tests")
    lead.get(f"/chat/{CHAT_B}")
    assert _log(database, "view_chat") == [("lead@example.com", CHAT_A)] * 2 + [
        ("lead@example.com", CHAT_B)
    ]
    assert [row[0] for row in _log(database, "login")] == ["lead@example.com"]


def test_the_access_log_page_is_for_admins_and_auditors(app: Starlette) -> None:
    lead_page = _as(app, "lead@example.com").get("/access-log")
    assert lead_page.status_code == 403
    assert _as(app, "ana.dev@example.com").get("/access-log").status_code == 403
    _as(app, "lead@example.com").get(f"/chat/{CHAT_A}")
    for email in ("auditor@example.com", "admin@example.com"):
        page = _as(app, email).get("/access-log")
        assert page.status_code == 200 and "view_chat" in page.text and CHAT_A in page.text


# ---------------------------------------------------------------- pages


def test_home_shows_the_tree_with_counts(app: Starlette, site: dict[str, Any]) -> None:
    page = _as(app, "lead@example.com").get("/").text
    assert "billing" in page and "ana@example.com" in page and "ben@example.com" in page
    assert f"/project/{site['project']}?account={site['ana']}" in page


def test_the_project_page_hides_empty_chats_until_asked(
    app: Starlette, site: dict[str, Any]
) -> None:
    lead = _as(app, "lead@example.com")
    url = f"/project/{site['project']}?account={site['ana']}"
    assert '<span class="badge">empty</span>' not in lead.get(url).text
    shown = lead.get(url + "&show_empty=1").text
    assert '<span class="badge">empty</span>' in shown
    assert "Fix the login bug" in shown
    assert lead.get("/project/99999").status_code == 404


def test_the_project_page_lists_counts(app: Starlette, site: dict[str, Any]) -> None:
    page = (
        _as(app, "lead@example.com").get(f"/project/{site['project']}?account={site['ana']}").text
    )
    assert re.search(r'<span class="ok">3</span> / <span class="bad">1</span>', page)


@pytest.mark.parametrize(
    ("tab", "expected"),
    [
        ("conversation", "I fixed the bug."),
        ("sessions", "startup"),
        ("decisions", "approved by AI (claude-opus-5-5)"),
        ("tests", "test_login"),
        ("code", "src/login.py"),
        ("corrections", "fixed after a failing run"),
        ("agents", "review-flow"),
        ("thinking", "Let me think about it"),
        ("usage", "xhigh"),
    ],
)
def test_every_tab_renders_its_data(app: Starlette, tab: str, expected: str) -> None:
    page = _as(app, "lead@example.com").get(f"/chat/{CHAT_A}?tab={tab}")
    assert page.status_code == 200 and expected in page.text


def test_the_conversation_summarises_tools_and_escapes_text(app: Starlette) -> None:
    text = _as(app, "lead@example.com").get(f"/chat/{CHAT_A}").text
    assert XSS not in text and "&lt;script&gt;" in text
    assert "Bash: pytest -q" in text
    assert "tool result: failed" in text
    assert "SECRET OUTPUT" not in text  # tool output is not shown
    assert "sub-agent text" not in text  # sub-agents have their own tab
    assert "rewound" in text and "/model" in text


def test_an_unknown_tab_shows_the_conversation(app: Starlette) -> None:
    assert "I fixed the bug." in _as(app, "lead@example.com").get(f"/chat/{CHAT_A}?tab=bogus").text


def test_the_site_works_under_a_mount_path(app: Starlette) -> None:
    mounted = Starlette(routes=[Mount("/explorer", app=app)])
    browser = Browser(mounted, "/explorer")
    login = browser.login("lead@example.com")
    assert login.status_code == 303 and login.headers["location"] == "/explorer/"
    assert "path=/explorer" in login.headers["set-cookie"].lower()
    home = browser.get("/")
    assert home.status_code == 200 and 'href="/explorer/project/' in home.text
    assert 'href="/explorer/style.css"' in home.text


# ---------------------------------------------------------------- reviewing decisions


def _decision(database: DatabaseSettings, seq: int) -> tuple[Any, ...]:
    return _sql(
        database,
        "SELECT decision_id, status, reviewed_by_kind, reviewer, review_note, review_history "
        "FROM decision WHERE session_id = %s AND seq = %s",
        CHAT_A, seq,
    )[0]  # fmt: skip


def test_a_lead_approves_and_rejects_with_a_note(
    app: Starlette, database: DatabaseSettings
) -> None:
    lead = _as(app, "lead@example.com")
    page = lead.get(f"/chat/{CHAT_A}?tab=decisions").text
    assert "Approve" in page
    decision_id = _decision(database, 2)[0]
    response = lead.post(
        f"/chat/{CHAT_A}/decisions/{decision_id}/review", page, status="approve", note="Looks right"
    )
    assert response.status_code == 303 and "tab=decisions" in response.headers["location"]
    _, status, kind, reviewer, note, history = _decision(database, 2)
    assert (status, kind, reviewer, note) == ("approved", "human", "Lead", "Looks right")
    history = history if isinstance(history, list) else json.loads(history)
    assert (
        len(history) == 1
        and history[0]["by_kind"] == "human"
        and history[0]["status"] == "approved"
    )
    assert (
        history[0]["reviewer"] == "Lead"
        and history[0]["note"] == "Looks right"
        and history[0]["at"]
    )

    shown = lead.get(f"/chat/{CHAT_A}?tab=decisions").text
    assert "approved by Lead" in shown and "Decision updated" not in shown

    lead.post(f"/chat/{CHAT_A}/decisions/{decision_id}/review", shown, status="reject", note="")
    _, status, _, _, _, history = _decision(database, 2)
    assert status == "rejected"
    assert len(history if isinstance(history, list) else json.loads(history)) == 2
    assert _log(database, "review_decision")[-1] == (
        "lead@example.com",
        f"{CHAT_A}#{decision_id}:rejected",
    )


def test_the_review_is_refused_for_developers_and_bad_input(
    app: Starlette, database: DatabaseSettings
) -> None:
    decision_id = _decision(database, 2)[0]
    path = f"/chat/{CHAT_A}/decisions/{decision_id}/review"
    ana = _as(app, "ana.dev@example.com")
    page = ana.get(f"/chat/{CHAT_A}?tab=decisions").text
    assert "Approve" not in page
    assert ana.post(path, page, status="approve").status_code == 403
    lead = _as(app, "lead@example.com")
    page = lead.get(f"/chat/{CHAT_A}?tab=decisions").text
    assert lead.post(path, page, status="maybe").status_code == 400
    assert lead.post(path, page, status="approve", note="x" * 501).status_code == 400
    assert (
        lead.post(f"/chat/{CHAT_A}/decisions/999999/review", page, status="approve").status_code
        == 404
    )
    other = _sql(database, "SELECT decision_id FROM decision WHERE session_id = %s", CHAT_B)[0][0]
    assert (
        lead.post(f"/chat/{CHAT_A}/decisions/{other}/review", page, status="approve").status_code
        == 404
    )
    assert _decision(database, 2)[1] == "needs_review"
    assert (
        _sql(database, "SELECT status FROM decision WHERE decision_id = %s", other)[0][0] == "draft"
    )


def test_the_decisions_package_is_used_when_it_exists(
    app: Starlette, database: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []
    fake = types.ModuleType("ccp.decisions")
    fake.set_decision_status = lambda *args: calls.append(args)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ccp.decisions", fake)
    lead = _as(app, "lead@example.com")
    page = lead.get(f"/chat/{CHAT_A}?tab=decisions").text
    decision_id = _decision(database, 2)[0]
    lead.post(f"/chat/{CHAT_A}/decisions/{decision_id}/review", page, status="reject", note="no")
    assert len(calls) == 1
    assert calls[0][1:] == (decision_id, "rejected", "Lead", "no")
    assert (
        _decision(database, 2)[1] == "needs_review"
    )  # only the package changed it (here: nothing)


# ---------------------------------------------------------------- administration


def test_only_admins_reach_the_admin_pages(app: Starlette) -> None:
    for email in ("ana.dev@example.com", "lead@example.com", "auditor@example.com"):
        browser = _as(app, email)
        assert browser.get("/admin/users").status_code == 403
        assert browser.get("/admin/labels").status_code == 403
        assert browser.client.post("/admin/users", data={}).status_code == 403
    assert _as(app, "admin@example.com").get("/admin/users").status_code == 200


def test_an_admin_creates_a_user_who_can_then_log_in(
    app: Starlette, database: DatabaseSettings
) -> None:
    admin = _as(app, "admin@example.com")
    page = admin.get("/admin/users").text
    created = admin.post("/admin/users", page, email="new@example.com", role="auditor", password="a brand new password", full_name="Nia", account_id="")  # fmt: skip  # noqa: S106
    assert created.status_code == 303
    assert _sql(
        database, "SELECT role, full_name FROM ui_user WHERE email = 'new@example.com'"
    ) == [("auditor", "Nia")]
    assert Browser(app).login("new@example.com", "a brand new password").status_code == 303
    duplicate = admin.post(
        "/admin/users",
        page,
        email="NEW@example.com",
        role="lead",
        password="another password!!",  # noqa: S106
    )
    assert duplicate.status_code == 400 and "already exists" in duplicate.text
    short = admin.post("/admin/users", page, email="x@example.com", role="lead", password="short")  # noqa: S106
    assert short.status_code == 400
    assert (
        admin.post(
            "/admin/users", page, email="y@example.com", role="boss", password=PASSWORD
        ).status_code
        == 400
    )


def test_an_admin_changes_role_account_and_state(
    app: Starlette, database: DatabaseSettings, site: dict[str, Any]
) -> None:
    admin = _as(app, "admin@example.com")
    page = admin.get("/admin/users").text
    ben = _sql(database, "SELECT user_id FROM ui_user WHERE email = 'ben.dev@example.com'")[0][0]
    assert admin.post(f"/admin/users/{ben}/role", page, role="lead").status_code == 303
    assert (
        admin.post(f"/admin/users/{ben}/account", page, account_id=str(site["ana"])).status_code
        == 303
    )
    assert _sql(database, "SELECT role, account_id FROM ui_user WHERE user_id = %s", ben) == [
        ("lead", site["ana"])
    ]
    assert admin.post(f"/admin/users/{ben}/account", page, account_id="").status_code == 303
    assert _sql(database, "SELECT account_id FROM ui_user WHERE user_id = %s", ben) == [(None,)]
    assert admin.post(f"/admin/users/{ben}/account", page, account_id="999999").status_code == 400
    assert admin.post(f"/admin/users/{ben}/role", page, role="boss").status_code == 400
    assert admin.post("/admin/users/999999/role", page, role="lead").status_code == 400


def test_an_admin_cannot_lock_themselves_out(app: Starlette, database: DatabaseSettings) -> None:
    admin = _as(app, "admin@example.com")
    page = admin.get("/admin/users").text
    me = _sql(database, "SELECT user_id FROM ui_user WHERE email = 'admin@example.com'")[0][0]
    assert admin.post(f"/admin/users/{me}/role", page, role="lead").status_code == 400
    assert admin.post(f"/admin/users/{me}/disable", page, disabled="1").status_code == 400
    assert _sql(database, "SELECT role, disabled FROM ui_user WHERE user_id = %s", me) == [
        ("admin", False)
    ]


def test_disabling_a_user_ends_their_session(app: Starlette, database: DatabaseSettings) -> None:
    lead = _as(app, "lead@example.com")
    assert lead.get("/").status_code == 200
    admin = _as(app, "admin@example.com")
    page = admin.get("/admin/users").text
    uid = _sql(database, "SELECT user_id FROM ui_user WHERE email = 'lead@example.com'")[0][0]
    assert admin.post(f"/admin/users/{uid}/disable", page, disabled="1").status_code == 303
    assert lead.get("/").status_code == 303
    assert Browser(app).login("lead@example.com").status_code == 401
    admin.post(f"/admin/users/{uid}/disable", page, disabled="0")
    assert Browser(app).login("lead@example.com").status_code == 303


def test_a_password_reset_ends_old_sessions_and_changes_the_password(
    app: Starlette, database: DatabaseSettings
) -> None:
    lead = _as(app, "lead@example.com")
    admin = _as(app, "admin@example.com")
    page = admin.get("/admin/users").text
    uid = _sql(database, "SELECT user_id FROM ui_user WHERE email = 'lead@example.com'")[0][0]
    assert admin.post(f"/admin/users/{uid}/password", page, password="short").status_code == 400  # noqa: S106
    assert lead.get("/").status_code == 200
    assert (
        admin.post(f"/admin/users/{uid}/password", page, password="the new password").status_code  # noqa: S106
        == 303
    )
    assert lead.get("/").status_code == 303
    assert Browser(app).login("lead@example.com", PASSWORD).status_code == 401
    assert Browser(app).login("lead@example.com", "the new password").status_code == 303


def test_an_admin_sets_and_removes_labels(
    app: Starlette, database: DatabaseSettings, site: dict[str, Any]
) -> None:
    admin = _as(app, "admin@example.com")
    page = admin.get("/admin/labels").text
    assert "ana@example.com" in page and "billing" in page
    assert (
        admin.post(
            "/admin/labels", page, kind="account", key="ana@example.com", label="claude1"
        ).status_code
        == 303
    )
    assert (
        admin.post(
            "/admin/labels",
            page,
            kind="project",
            key="github.com/acme/billing",
            label="billing-api",
        ).status_code
        == 303
    )
    assert _sql(
        database, "SELECT label FROM claude_account WHERE account_id = %s", site["ana"]
    ) == [("claude1",)]
    assert _sql(database, "SELECT label FROM project WHERE project_id = %s", site["project"]) == [
        ("billing-api",)
    ]
    assert "billing-api" in _as(app, "lead@example.com").get("/").text
    assert (
        admin.post(
            "/admin/labels", page, kind="account", key="ana@example.com", label=""
        ).status_code
        == 303
    )
    assert _sql(
        database, "SELECT label FROM claude_account WHERE account_id = %s", site["ana"]
    ) == [(None,)]
    bad = admin.post(
        "/admin/labels", page, kind="account", key="ana@example.com", label="no <tags> allowed"
    )
    assert bad.status_code == 400 and "letters, digits" in bad.text
    assert admin.post("/admin/labels", page, kind="other", key="x", label="y").status_code == 400


def test_the_session_cookie_is_not_accepted_with_another_secret(
    app: Starlette, database: DatabaseSettings
) -> None:
    lead = _as(app, "lead@example.com")
    other = TestClient(
        create_app(database, secret="a-different-invented-secret"),  # noqa: S106
        follow_redirects=False,
    )
    other.cookies.update(lead.client.cookies)
    assert other.get("/").status_code == 303


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the real decisions package out of the way unless a test installs a fake one."""
    monkeypatch.setitem(sys.modules, "ccp.decisions", types.ModuleType("ccp.decisions"))
    yield


def test_the_command_line_creates_a_user_who_can_log_in(
    app: Starlette,
    database: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ccp.explorer.cli import main

    monkeypatch.setenv("POSTGRES_HOST", database.host)
    monkeypatch.setenv("POSTGRES_PORT", str(database.port))
    monkeypatch.setenv("POSTGRES_DB", database.name)
    monkeypatch.setenv("POSTGRES_USER", database.user)
    monkeypatch.setenv("POSTGRES_PASSWORD", database.password.reveal())
    monkeypatch.setenv("CCP_EXPLORER_PASSWORD", "a password from the environment")
    args = [
        "create-user",
        "Cli@Example.com",
        "--role",
        "developer",
        "--account-email",
        "ana@example.com",
    ]
    assert main(args) == 0
    assert "cli@example.com" in capsys.readouterr().out
    assert _sql(
        database, "SELECT role, account_id IS NOT NULL FROM ui_user WHERE email = 'cli@example.com'"
    ) == [("developer", True)]
    assert (
        Browser(app).login("cli@example.com", "a password from the environment").status_code == 303
    )
    assert main(args) == 1  # the same address again
    assert "already exists" in capsys.readouterr().err
    monkeypatch.setenv("CCP_EXPLORER_PASSWORD", "short")
    assert main(["create-user", "b@example.com", "--role", "lead"]) == 1
