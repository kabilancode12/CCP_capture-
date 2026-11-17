from datetime import UTC, datetime

from ccp.explorer import pages
from ccp.explorer.queries import as_json_list
from ccp.explorer.users import UiUser

VIEW = pages.View("/explorer", None, "token")


def _user(role: str, account_id: int | None = None) -> UiUser:
    return UiUser(1, "a@example.com", "Ana", role, account_id, None, False)


def test_every_value_is_escaped() -> None:
    assert pages.esc("<b>&\"'") == "&lt;b&gt;&amp;&quot;&#x27;"
    assert pages.esc(None) == ""
    html = pages.table(["h"], [[pages.esc("<script>")]])
    assert "<script>" not in html


def test_links_use_the_mount_path_and_skip_empty_query_values() -> None:
    assert VIEW.url("/chat/x", tab="tests", page=None) == "/explorer/chat/x?tab=tests"
    assert pages.View("", None, "").url("/") == "/"


def test_a_query_value_cannot_break_out_of_an_attribute() -> None:
    assert '"' not in pages.View("", None, "").url("/p", q='"><script>')


def test_post_forms_carry_the_csrf_token() -> None:
    form = pages.post_form(VIEW, "/logout", "")
    assert 'name="csrf" value="token"' in form
    assert 'method="post"' in form


def test_account_names_are_neutral_for_developers() -> None:
    row = {"account_label": None, "account_name": "Ana Dev", "account_email": "ana@example.com"}
    assert pages.account_title(row, privileged=True) == "Ana Dev"
    neutral = pages.account_title(row, privileged=False)
    assert neutral.startswith("account-") and "ana" not in neutral
    assert pages.account_title({**row, "account_label": "claude1"}, privileged=False) == "claude1"
    assert pages.account_title({}, privileged=False) == "unknown account"


def test_project_names_fall_back_in_order() -> None:
    assert pages.project_title({"project_label": "billing", "project_name": "x"}) == "billing"
    assert pages.project_title({"project_name": "ccp_V2"}) == "ccp_V2"
    assert pages.project_title({"folder": "c:\\work\\app"}) == "app"
    assert pages.project_title({}) == "unknown project"


def test_tool_calls_are_one_line() -> None:
    assert pages.summarise_call("Bash", {"command": "pytest -q"}) == "Bash: pytest -q"
    assert (
        pages.summarise_call("Edit", {"file_path": "/a/b.py", "old_string": "x"}) == "Edit: /a/b.py"
    )
    assert pages.summarise_call("Mystery", {"x": 1}) == "Mystery"
    assert pages.summarise_call("Mystery", None) == "Mystery"
    assert len(pages.summarise_call("Bash", {"command": "x" * 1000})) < 200


def test_numbers_and_times_have_a_fixed_format() -> None:
    assert pages.num(1234567) == "1,234,567"
    assert pages.num(None) == "-"
    assert pages.when(datetime(2026, 11, 15, 9, 5, tzinfo=UTC)) == "2026-11-15 09:05"
    assert pages.when(None) == "-"


def test_json_lists_are_read_from_text_too() -> None:
    assert as_json_list('["a", "b"]') == ["a", "b"]
    assert as_json_list(["a"]) == ["a"]
    assert as_json_list("not json") == []
    assert as_json_list(None) == []
    assert as_json_list('{"a": 1}') == []


def test_a_decision_says_who_reviewed_it() -> None:
    base = {"status": "approved", "reviewer": "claude-opus-5-5"}
    assert "by AI (claude-opus-5-5)" in pages._who_reviewed({**base, "reviewed_by_kind": "ai"})
    human = {"status": "rejected", "reviewer": "Ana", "reviewed_by_kind": "human"}
    assert pages._who_reviewed(human) == "rejected by Ana"
    draft = {"status": "needs_review", "reviewer": None, "reviewed_by_kind": None}
    assert "not reviewed" in pages._who_reviewed(draft)


def test_roles_decide_what_a_user_may_do() -> None:
    dev = _user("developer", 5)
    assert dev.may_open_chat(5) and not dev.may_open_chat(6) and not dev.may_open_chat(None)
    assert not _user("developer", None).may_open_chat(None)
    for role in ("lead", "auditor", "admin"):
        assert _user(role).may_open_chat(99) and _user(role).may_review
    assert not dev.may_review and not dev.reads_access_log and not dev.is_admin
    assert _user("auditor").reads_access_log and not _user("lead").reads_access_log
    assert _user("admin").is_admin and not _user("auditor").is_admin
    assert dev.reviewer_name == "Ana"
