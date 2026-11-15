"""What a changed file is for, and how many tests an edit added.

Two small rules, applied to every file change the indexer records:

* :func:`file_role` sorts a path into ``test``, ``docs``, ``config``, ``code`` or ``other``, by the
  path alone (folder names and file names). The first rule that matches wins, in this order: test,
  docs, config, code. So ``tests/README.md`` is a test file and ``docs/conf.py`` is documentation.
* :func:`count_tests_added` counts the added lines that *define* a test (``def test_x``, ``it(``,
  ``@Test``, ``func TestX``, ``#[test]`` ...). It looks at the start of each line only, so a call
  such as ``result = test(1)`` or a word inside a string is not counted.

Both are plain rules, not a parser: they are cheap, predictable, and good enough to answer "did this
chat add tests?". Folder rules are case-sensitive (``tests``), file-name rules are not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ROLE_TEST = "test"
ROLE_DOCS = "docs"
ROLE_CONFIG = "config"
ROLE_CODE = "code"
ROLE_OTHER = "other"

_TEST_FOLDERS = frozenset({"tests", "test", "__tests__"})
_DOCS_FOLDERS = frozenset({"docs", "doc"})

_TEST_NAME = re.compile(
    r"^(?:test_.*\.py|.*_test\.py|conftest\.py|.*_test\.go|.*\.(?:spec|test)\.[^.]+"
    r"|(?-i:.*Tests?\.(?:java|kt|cs))|.*_spec\.rb)$",
    re.IGNORECASE,
)
_DOCS_NAME = re.compile(r"^.*\.(?:md|mdx|rst|adoc)$", re.IGNORECASE)
_CONFIG_NAME = re.compile(
    r"^(?:.*\.(?:toml|ya?ml|json|ini|cfg)|\.env.*|Dockerfile.*)$",
    re.IGNORECASE,
)
_CODE_NAME = re.compile(
    r"^.*\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|kt|kts|scala|cs|c|h|cc|cpp|hpp|swift|rb|php"
    r"|sh|bash|ps1|sql|html|css|scss|vue|svelte|lua|dart|ex|exs|r|m)$",
    re.IGNORECASE,
)

# A line that defines a test, matched at the start of the line (after blanks).
_TEST_DEFINITION = re.compile(
    r"^\s*(?:"
    r"(?:async\s+)?def\s+test_\w*\s*\("  # Python: def test_x(, async def test_x(
    r"|(?:it|test|describe)(?:\.(?:only|skip|each|todo|concurrent))?\s*\("  # JavaScript
    r"|@Test\b"  # Java, Kotlin, C# (xUnit uses [Fact], not covered)
    r"|func\s+Test\w*\s*\("  # Go
    r"|#\[(?:\w+::)?test\b"  # Rust: #[test], #[tokio::test]
    r")"
)


def file_role(path: str) -> str:
    r"""Return what a file is for: ``test``, ``docs``, ``config``, ``code`` or ``other``.

    Args:
        path: The file's path; ``/`` or a backslash may separate the parts.
    """
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    if not parts:
        return ROLE_OTHER
    folders, name = parts[:-1], parts[-1]
    if _TEST_FOLDERS.intersection(folders) or _TEST_NAME.match(name):
        return ROLE_TEST
    if _DOCS_FOLDERS.intersection(folders) or _DOCS_NAME.match(name):
        return ROLE_DOCS
    if _CONFIG_NAME.match(name):
        return ROLE_CONFIG
    if _CODE_NAME.match(name):
        return ROLE_CODE
    return ROLE_OTHER


def count_tests_added(added_lines: Iterable[str]) -> int:
    """Count the added lines that define a test.

    Args:
        added_lines: The text of each added line, without the diff's ``+``.
    """
    return sum(1 for line in added_lines if _TEST_DEFINITION.match(line))
