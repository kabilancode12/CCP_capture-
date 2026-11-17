"""The display name of a session."""

from ccp.formatters import format_session_name


def test_a_session_name_has_its_number_and_title() -> None:
    assert format_session_name(1, "Add Login Endpoint") == "Session 1: Add Login Endpoint"


def test_extra_white_space_is_removed_and_an_empty_title_leaves_only_the_number() -> None:
    assert format_session_name(12, "  Add   Login\nEndpoint ") == "Session 12: Add Login Endpoint"
    assert format_session_name(3, "   ") == "Session 3"
