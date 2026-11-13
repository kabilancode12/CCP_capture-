"""Finding file changes in transcript entries, and tracing them to the prompt behind them."""

from typing import Any

import pytest

from ccp.index.filechanges import FileChange, extract_file_change
from ccp.index.tree import TreeNode, nearest_prompts


def _result(result: object, cwd: str | None = "/work/app", tool_use_id: str = "toolu_1") -> Any:
    entry: dict[str, Any] = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
        "toolUseResult": result,
    }
    if cwd is not None:
        entry["cwd"] = cwd
    return entry


EDIT = {
    "filePath": "/work/app/src/a.py",
    "oldString": "x",
    "newString": "y",
    "originalFile": "whole file",
    "structuredPatch": [
        {
            "oldStart": 10,
            "oldLines": 3,
            "newStart": 10,
            "newLines": 4,
            "lines": [" keep", "-old one", "+new one", "+new two", " keep"],
        }
    ],
    "userModified": False,
    "replaceAll": False,
}


def test_an_edit_is_read_with_its_line_ranges_and_counts() -> None:
    change = extract_file_change(_result(EDIT))

    assert change == FileChange(
        tool_use_id="toolu_1",
        path="src/a.py",
        absolute_path="/work/app/src/a.py",
        change_type="modified",
        user_modified=False,
        lines_added=2,
        lines_removed=1,
        hunks=({"old_start": 10, "old_lines": 3, "new_start": 10, "new_lines": 4},),
    )


def test_a_new_file_has_no_patch_so_the_whole_content_is_the_change() -> None:
    write: dict[str, Any] = {
        "type": "create",
        "filePath": "/work/app/new.py",
        "content": "a\nb\nc\n",
        "structuredPatch": [],
        "originalFile": None,
    }

    change = extract_file_change(_result(write))

    assert change is not None
    assert (change.change_type, change.lines_added, change.lines_removed) == ("added", 3, 0)
    assert change.hunks == ({"old_start": 0, "old_lines": 0, "new_start": 1, "new_lines": 3},)


def test_an_empty_new_file_has_no_hunk() -> None:
    write = {
        "type": "create",
        "filePath": "/work/app/empty.py",
        "content": "",
        "structuredPatch": [],
    }

    change = extract_file_change(_result(write))

    assert change is not None
    assert (change.hunks, change.lines_added) == ((), 0)


def test_overwriting_a_file_is_a_modification_with_its_patch() -> None:
    update = {**EDIT, "type": "update"}
    change = extract_file_change(_result(update))
    assert change is not None
    assert change.change_type == "modified"


@pytest.mark.parametrize(
    "result",
    [
        None,
        "Error: the file does not exist",  # a failed tool gives a plain string
        {"type": "text", "file": {"filePath": "/work/app/a.py"}},  # a Read result
        {"filePath": "/work/app/a.py"},  # no patch
        {"filePath": "", "structuredPatch": []},
        {"filePath": 5, "structuredPatch": []},
        {"filePath": "/work/app/a.py", "structuredPatch": "none"},
        [1, 2],
    ],
)
def test_results_that_are_not_edits_are_ignored(result: object) -> None:
    assert extract_file_change(_result(result)) is None


def test_a_malformed_hunk_is_left_out_and_the_rest_is_kept() -> None:
    patch = [
        {"oldStart": "x"},
        "junk",
        {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1},
    ]
    change = extract_file_change(_result({"filePath": "/work/app/a.py", "structuredPatch": patch}))
    assert change is not None
    assert change.hunks == ({"old_start": 1, "old_lines": 1, "new_start": 1, "new_lines": 1},)


@pytest.mark.parametrize(
    ("path", "cwd", "expected"),
    [
        ("/work/app/src/a.py", "/work/app", "src/a.py"),
        ("/work/app/src/a.py", "/work/app/", "src/a.py"),
        ("/work/other/a.py", "/work/app", "/work/other/a.py"),
        ("/work/application/a.py", "/work/app", "/work/application/a.py"),
        ("C:\\Users\\dev\\app\\src\\a.py", "c:\\users\\dev\\app", "src/a.py"),
        ("C:\\Users\\dev\\other\\a.py", "C:\\Users\\dev\\app", "C:/Users/dev/other/a.py"),
        ("/Work/app/a.py", "/work/app", "/Work/app/a.py"),  # Unix paths are case sensitive
        ("/work/app/a.py", None, "/work/app/a.py"),
    ],
)
def test_the_path_is_relative_to_the_session_folder_when_inside_it(
    path: str, cwd: str | None, expected: str
) -> None:
    result = {"filePath": path, "structuredPatch": []}
    change = extract_file_change(_result(result, cwd=cwd))
    assert change is not None
    assert (change.path, change.absolute_path) == (expected, path)


def test_the_call_id_is_the_first_tool_result_block() -> None:
    entry = _result(EDIT)
    entry["message"]["content"] = [{"type": "text"}, {"type": "tool_result", "tool_use_id": "t7"}]
    change = extract_file_change(entry)
    assert change is not None
    assert change.tool_use_id == "t7"


def test_user_modified_is_unknown_unless_it_is_a_boolean() -> None:
    change = extract_file_change(_result({**EDIT, "userModified": "yes"}))
    assert change is not None
    assert change.user_modified is None


# ---------------------------------------------------------------- tracing to a prompt


def _node(
    event_id: int,
    uuid: str,
    parent: str | None,
    *,
    prompt: bool = False,
    stream: str = "main",
) -> TreeNode:
    return TreeNode(event_id, uuid, parent, None, prompt, (f"sessions/s1/{stream}/000", event_id))


def test_an_edit_is_traced_up_to_the_prompt_that_led_to_it() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "a1", "p1"),
        _node(3, "r1", "a1"),
        _node(4, "p2", "r1", prompt=True),
        _node(5, "a2", "p2"),
        _node(6, "r2", "a2"),
    ]
    assert nearest_prompts(nodes, [3, 6], {}) == {3: 1, 6: 4}


def test_a_sub_agent_edit_is_traced_through_the_call_that_launched_the_agent() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "launch", "p1"),  # the parent's Agent call
        _node(3, "s1", None, stream="agent-a1"),  # the sub-agent's first entry (not a human prompt)
        _node(4, "s2", "s1", stream="agent-a1"),
        _node(5, "edit", "s2", stream="agent-a1"),
    ]
    assert nearest_prompts(nodes, [5], {"a1": 2}) == {5: 1}


def test_a_nested_sub_agent_is_traced_through_both_launches() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "launch1", "p1"),
        _node(3, "s1", None, stream="agent-a1"),
        _node(4, "launch2", "s1", stream="agent-a1"),
        _node(5, "t1", None, stream="agent-b2"),
        _node(6, "edit", "t1", stream="agent-b2"),
    ]
    assert nearest_prompts(nodes, [6], {"a1": 2, "b2": 4}) == {6: 1}


def test_a_chain_with_no_prompt_gives_none() -> None:
    nodes = [_node(1, "a1", None), _node(2, "r1", "a1")]
    assert nearest_prompts(nodes, [2], {}) == {2: None}


def test_an_agent_that_was_never_matched_to_a_launch_gives_none() -> None:
    nodes = [_node(1, "s1", None, stream="agent-a1"), _node(2, "edit", "s1", stream="agent-a1")]
    assert nearest_prompts(nodes, [2], {}) == {2: None}


def test_a_loop_in_the_data_ends_instead_of_hanging() -> None:
    nodes = [_node(1, "a", "b"), _node(2, "b", "a")]
    assert nearest_prompts(nodes, [1], {}) == {1: None}


def test_an_unknown_start_gives_none() -> None:
    assert nearest_prompts([], [99], {}) == {99: None}


def test_a_compaction_boundary_is_followed_through_its_logical_parent() -> None:
    boundary = TreeNode(3, "b1", None, "r1", False, ("sessions/s1/main/000", 3))
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "r1", "p1"),
        boundary,
        _node(4, "edit", "b1"),
    ]
    assert nearest_prompts(nodes, [4], {}) == {4: 1}
