"""Linking a commit to the sessions that wrote its lines."""

from datetime import UTC, datetime, timedelta

import pytest

from ccp.audit.gitlog import parse_added_lines
from ccp.audit.linking import AddedLines, Link, link_commit, same_file, significant

COMMIT_TIME = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
BEFORE = COMMIT_TIME - timedelta(hours=1)


def _ai(session: str, path: str, *texts: str, when: datetime | None = BEFORE) -> list[AddedLines]:
    return [AddedLines(session, path, text, when) for text in texts]


LINES = ["def total(items):", "    return sum(i.price for i in items)", "print(total(cart))"]


def test_a_commit_written_by_one_session_is_linked_to_it_in_full() -> None:
    links = link_commit({"src/a.py": LINES}, COMMIT_TIME, _ai("s1", "src/a.py", *LINES))

    assert links == [Link("s1", 1.0, 3, 3)]


def test_a_commit_shared_by_two_sessions_links_both_with_their_shares() -> None:
    ai = _ai("s1", "src/a.py", LINES[0], LINES[1]) + _ai("s2", "src/a.py", LINES[2])

    links = link_commit({"src/a.py": LINES}, COMMIT_TIME, ai, min_confidence=0.2)

    assert links == [Link("s1", 0.667, 2, 3), Link("s2", 0.333, 1, 3)]


def test_a_session_that_explains_too_little_is_not_linked() -> None:
    ai = _ai("s1", "src/a.py", LINES[0])

    assert link_commit({"src/a.py": LINES}, COMMIT_TIME, ai, min_confidence=0.5) == []


def test_a_commit_the_ai_did_not_write_is_not_linked() -> None:
    ai = _ai("s1", "src/a.py", "something else entirely", "and another line of it")

    assert link_commit({"src/a.py": LINES}, COMMIT_TIME, ai) == []


def test_the_same_text_in_another_file_does_not_count() -> None:
    ai = _ai("s1", "src/other.py", *LINES)

    assert link_commit({"src/a.py": LINES}, COMMIT_TIME, ai) == []


def test_short_lines_are_ignored_on_both_sides() -> None:
    commit = ["}", "", "    ", "end", "x = 1"]
    ai = _ai("s1", "src/a.py", "}", "", "end")

    assert link_commit({"src/a.py": commit}, COMMIT_TIME, ai) == []
    # Nothing significant to compare: no link, and no division by zero.
    assert link_commit({"src/a.py": ["}", ""]}, COMMIT_TIME, ai) == []


def test_surrounding_space_does_not_matter() -> None:
    links = link_commit(
        {"a.py": ["    return value  "]}, COMMIT_TIME, _ai("s1", "a.py", "return value")
    )

    assert links == [Link("s1", 1.0, 1, 1)]


def test_a_line_is_explained_as_often_as_the_session_added_it() -> None:
    commit = {"a.py": ["print('done')"] * 3}

    once = link_commit(commit, COMMIT_TIME, _ai("s1", "a.py", "print('done')"), min_confidence=0.1)
    thrice = link_commit(commit, COMMIT_TIME, _ai("s1", "a.py", *["print('done')"] * 3))

    assert once == [Link("s1", 0.333, 1, 3)]
    assert thrice == [Link("s1", 1.0, 3, 3)]


def test_edits_made_after_the_commit_do_not_count() -> None:
    late = COMMIT_TIME + timedelta(hours=1)

    assert link_commit({"a.py": LINES}, COMMIT_TIME, _ai("s1", "a.py", *LINES, when=late)) == []


def test_a_little_clock_difference_is_tolerated() -> None:
    just_after = COMMIT_TIME + timedelta(seconds=120)

    links = link_commit({"a.py": LINES}, COMMIT_TIME, _ai("s1", "a.py", *LINES, when=just_after))

    assert [link.session_id for link in links] == ["s1"]


def test_edits_without_a_time_count() -> None:
    links = link_commit({"a.py": LINES}, COMMIT_TIME, _ai("s1", "a.py", *LINES, when=None))

    assert [link.session_id for link in links] == ["s1"]


def test_a_commit_without_a_time_accepts_every_edit() -> None:
    late = COMMIT_TIME + timedelta(days=9)

    assert link_commit({"a.py": LINES}, None, _ai("s1", "a.py", *LINES, when=late)) != []


def test_an_empty_commit_links_to_nothing() -> None:
    assert link_commit({}, COMMIT_TIME, _ai("s1", "a.py", *LINES)) == []


def test_the_most_confident_session_comes_first() -> None:
    ai = _ai("weak", "a.py", LINES[0]) + _ai("strong", "a.py", *LINES)

    links = link_commit({"a.py": LINES}, COMMIT_TIME, ai, min_confidence=0.1)

    assert [link.session_id for link in links] == ["strong", "weak"]


@pytest.mark.parametrize(
    ("commit_path", "edit_path", "same"),
    [
        ("src/a.py", "src/a.py", True),
        (
            "app/src/a.py",
            "src/a.py",
            True,
        ),  # the session ran in the repository root, the edit in app/
        ("src/a.py", "app/src/a.py", True),
        ("xsrc/a.py", "src/a.py", False),
        ("src/a.py", "src/b.py", False),
        ("a.py", "src/a.py", True),
        ("/src/a.py/", "src/a.py", True),
    ],
)
def test_paths_are_compared_on_folder_boundaries(
    commit_path: str, edit_path: str, same: bool
) -> None:
    assert same_file(commit_path, edit_path) is same


def test_a_line_must_be_long_enough_to_mean_something() -> None:
    assert significant("  x = 1  ") == "x = 1"
    assert significant(" }  ") is None
    assert significant("") is None


# ------------------------------------------------------------------ reading a diff


DIFF = """\
diff --git a/src/a.py b/src/a.py
index 111..222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,0 +2,2 @@ def old():
+first added
+second added
@@ -9 +11 @@
-removed
+changed
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+hello
++++ looks like a header but is content
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/img.png b/img.png
Binary files differ
"""


def test_the_added_lines_are_read_per_file() -> None:
    assert parse_added_lines(DIFF) == {
        "src/a.py": ["first added", "second added", "changed"],
        "new.txt": ["hello", "+++ looks like a header but is content"],
    }


def test_a_diff_with_no_changes_has_no_lines() -> None:
    assert parse_added_lines("") == {}


def test_carriage_returns_are_dropped_from_the_lines() -> None:
    patch = "+++ b/a.txt\n@@ -0,0 +1 @@\n+windows line\r\n"

    assert parse_added_lines(patch) == {"a.txt": ["windows line"]}
