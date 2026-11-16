from datetime import UTC, datetime, timedelta, timezone

from ccp.catalog.names import (
    CHAT_FOLDER_PATTERN,
    account_folder,
    chat_folder,
    chat_id8_of_folder,
    project_folder,
    short_id,
    slugify,
)


def test_slugify_keeps_only_lowercase_letters_digits_and_dashes() -> None:
    assert slugify("Fix the Bug (v2)!") == "fix-the-bug-v2"
    assert slugify("  --Ünïcode  títle--") == "unicode-title"


def test_slugify_uses_the_fallback_when_nothing_is_left() -> None:
    assert slugify("???", fallback="untitled") == "untitled"
    assert slugify(None, fallback="x") == "x"


def test_slugify_cuts_without_leaving_a_dash() -> None:
    assert slugify("aaaa bbbb cccc", 5) == "aaaa"
    assert len(slugify("word " * 40, 60)) <= 60


def test_short_id_is_the_first_eight_characters() -> None:
    assert short_id("895d43d3-ddd9-46a1-813f-f4d717d57db3") == "895d43d3"
    assert short_id("sess-story") == "sess-sto"


def test_account_folder_uses_label_then_name_and_the_uuid_start() -> None:
    uuid = "ABCD1234-0000-0000-0000-000000000000"
    assert account_folder(3, uuid, "Claude 1", "Real Name") == "claude-1__abcd1234"
    assert account_folder(3, uuid, None, "Real Name") == "real-name__abcd1234"
    assert account_folder(3, None, None, None) == "account__00000003"
    assert account_folder(None, None, None, None) == "unknown-account__00000000"


def test_project_folder_prefers_the_label() -> None:
    assert project_folder("Billing API", "billing") == "billing-api"
    assert project_folder(None, "ccp_V2") == "ccp-v2"
    assert project_folder(None, None) == "unknown-project"


def test_chat_folder_has_date_title_and_id8() -> None:
    started = datetime(2026, 7, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    # 23:30 at UTC-5 is already the next day in UTC.
    assert chat_folder(started, "Fix: the bug", "895d43d3-x") == "2026-07-02__fix-the-bug__895d43d3"
    assert chat_folder(None, None, "abc") == "undated__untitled__abc"
    assert (
        len(chat_folder(datetime(2026, 1, 1, tzinfo=UTC), "t " * 100, "abcdefgh").split("__")[1])
        <= 60
    )


def test_chat_id8_is_found_at_the_end_of_the_folder_name() -> None:
    assert chat_id8_of_folder("2026-07-02__fix-the-bug__895d43d3") == "895d43d3"


def test_folder_pattern_splits_a_key() -> None:
    key = "catalog/accounts/a__1/projects/p/sessions/2026-07-02__t__895d43d3/transcript/main.json"
    match = CHAT_FOLDER_PATTERN.match(key)
    assert match is not None
    assert match.group(1).endswith("chats/2026-07-02__t__895d43d3")
    assert match.group("rest") == "transcript/main.json"
    assert CHAT_FOLDER_PATTERN.match("catalog/index.json") is None
