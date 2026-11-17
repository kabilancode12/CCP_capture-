import pytest

from ccp.catalog.cli import build_parser, tree_lines

KEYS = [
    "catalog/README.md",
    "catalog/index.json",
    "catalog/accounts/claude1__abcd1234/projects/billing/chats/2026-07-01__fix__c1c1c1c1/README.md",
    "catalog/accounts/claude1__abcd1234/projects/billing/chats/2026-07-01__fix__c1c1c1c1/transcript/main.json",
    "catalog/accounts/claude1__abcd1234/projects/billing/chats/2026-07-02__more__d2d2d2d2/README.md",
]


def test_tree_shows_account_project_and_chats() -> None:
    assert tree_lines(KEYS, with_files=False) == [
        "catalog/",
        "  README.md",
        "  index.json",
        "  claude1__abcd1234",
        "    billing",
        "      2026-07-01__fix__c1c1c1c1",
        "      2026-07-02__more__d2d2d2d2",
    ]


def test_tree_can_list_files() -> None:
    lines = tree_lines(KEYS, with_files=True)
    assert "        README.md" in lines and "        transcript" in lines
    assert "          main.json" in lines


def test_build_needs_a_chat_or_all() -> None:
    parser = build_parser()
    assert parser.parse_args(["build", "--all"]).all is True
    assert parser.parse_args(["build", "--chat", "x"]).chat == "x"
    with pytest.raises(SystemExit):
        parser.parse_args(["build"])
    with pytest.raises(SystemExit):
        parser.parse_args(["build", "--all", "--chat", "x"])
