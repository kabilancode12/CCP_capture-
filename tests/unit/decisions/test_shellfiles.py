"""Which files a shell command changes: only the clear cases, never a guess."""

import pytest

from ccp.decisions.shellfiles import CREATED, DELETED, MODIFIED, ShellFile, files_changed_by


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat > Uploads.cs <<'EOF'\nusing X; int a = b > c;\nEOF", [("Uploads.cs", CREATED)]),
        ("echo hi >> log.txt", [("log.txt", MODIFIED)]),
        ("sed -i '/^using/d' Program.cs", [("Program.cs", MODIFIED)]),
        ("rm -f a.http b.http", [("a.http", DELETED), ("b.http", DELETED)]),
        ("mv a.cs b.cs", [("a.cs", DELETED), ("b.cs", CREATED)]),
        ("mkdir -p tools && touch tools/x.sh", [("tools", CREATED), ("tools/x.sh", CREATED)]),
        ("echo x | tee out.txt", [("out.txt", CREATED)]),
        ("dotnet new webapi -n Api", [("(project files from dotnet new webapi)", CREATED)]),
    ],
)
def test_the_clear_cases_are_read(command: str, expected: list[tuple[str, str]]) -> None:
    assert files_changed_by(command) == [ShellFile(p, c) for p, c in expected]


@pytest.mark.parametrize(
    "command",
    [
        "ls -la && pwd",
        "python3 run.py 2>&1 | tail -5",
        "echo hi > /tmp/scratch.txt",
        "cmd 2>/dev/null",
        "rm -rf $BUILD_DIR",
        "rm -f *.log",
        "echo 'unterminated",
    ],
)
def test_anything_unclear_or_outside_the_project_names_no_file(command: str) -> None:
    assert files_changed_by(command) == []


def test_the_last_change_of_a_file_wins_and_it_is_listed_once() -> None:
    assert files_changed_by("echo a > f.txt; rm f.txt") == [ShellFile("f.txt", DELETED)]


def test_paths_after_a_relative_cd_are_inside_that_folder_and_a_trailing_slash_is_not_a_second_file() -> (
    None
):
    from ccp.index.shellfiles import files_changed_by

    command = "mkdir -p src/ccp/server && cd src/ccp/server && touch __init__.py && cat > plugins.py <<'EOF'\nx\nEOF\n"
    paths = [f.path for f in files_changed_by(command)]
    assert "src/ccp/server/__init__.py" in paths and "src/ccp/server/plugins.py" in paths
    assert "__init__.py" not in paths and "src/ccp/server" in paths
    both = files_changed_by("mkdir -p ~/.config/u && touch ~/.config/u/ && echo x > ~/.config/u")
    assert [f.path for f in both].count("~/.config/u") == 1
    absolute = files_changed_by("cd /home/me/proj && cat > a.py <<'EOF'\nx\nEOF\n")
    assert [f.path for f in absolute] == ["a.py"]  # an absolute cd is left as it was
