"""How a chat's project is named from its Git remote or folder (no database needed)."""

import pytest

from ccp.db.hierarchy import (
    ProjectKey,
    folder_name,
    normalise_folder,
    normalise_git_remote,
    project_key,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/app.git",
        "https://user:secret@GitHub.com/Acme/App.git",
        "http://github.com/acme/app/",
        "ssh://git@github.com:22/acme/app.git",
        "git@github.com:acme/app.git",
        "github.com:acme/app",
    ],
)
def test_every_way_of_writing_a_remote_gives_the_same_key(url: str) -> None:
    assert normalise_git_remote(url) == "github.com/acme/app"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/srv/git/app.git", "local/srv/git/app"),
        ("file:///srv/git/app.git", "local/srv/git/app"),
        ("C:\\repos\\App.git", "local/c:/repos/app"),  # a Windows path, not a host called C
        ("https://gitlab.example.com/group/sub/app.git", "gitlab.example.com/group/sub/app"),
    ],
)
def test_other_remotes_are_normalised_too(url: str, expected: str) -> None:
    assert normalise_git_remote(url) == expected


@pytest.mark.parametrize("url", ["", "   ", "https://github.com/", "https://[bad/x"])
def test_an_unusable_remote_gives_none(url: str) -> None:
    assert normalise_git_remote(url) is None


def test_a_remote_names_the_project() -> None:
    key = project_key("git@github.com:acme/billing.git", "laptop-1", "/home/dev/billing")
    assert key == ProjectKey("github.com/acme/billing", "git_remote", "billing")


def test_without_a_remote_the_folder_on_its_machine_names_the_project() -> None:
    key = project_key(None, "Laptop-1", "/home/dev/ccp_V2")
    assert key == ProjectKey("laptop-1:/home/dev/ccp_V2", "folder", "ccp_V2")
    assert project_key("", None, "C:\\work\\app\\") == ProjectKey(
        "c:\\work\\app\\", "folder", "app"
    )  # the drive letter in lower case (see normalise_folder)


def test_without_remote_or_folder_there_is_no_project() -> None:
    assert project_key(None, "laptop-1", None) is None


@pytest.mark.parametrize(
    ("folder", "name"), [("/home/dev/ccp_V2", "ccp_V2"), ("/a/b/", "b"), ("/", "/"), ("x", "x")]
)
def test_the_name_of_a_folder_is_its_last_part(folder: str, name: str) -> None:
    assert folder_name(folder) == name


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("C:\\work\\app", "c:\\work\\app"),
        ("c:/work/app", "c:\\work\\app"),
        ("D:\\Work/App", "d:\\Work\\App"),  # only the drive letter changes case
        ("/home/Dev/app", "/home/Dev/app"),  # not Windows: unchanged
        ("relative/app", "relative/app"),
    ],
)
def test_a_windows_folder_is_keyed_in_one_spelling(folder: str, expected: str) -> None:
    assert normalise_folder(folder) == expected


def test_two_spellings_of_one_windows_folder_are_one_project() -> None:
    first = project_key(None, "Box", "C:\\work\\app")
    second = project_key(None, "box", "c:/work/app")
    assert first == second == ProjectKey("box:c:\\work\\app", "folder", "app")
