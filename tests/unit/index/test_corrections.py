"""The rules that find corrections (pure functions; the SQL is tested with a real database)."""

from datetime import UTC, datetime, timedelta

import pytest

from ccp.index.corrections import (
    Edit,
    ToolCall,
    find_command_retries,
    find_reworks,
    first_program,
)

START = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("command", "program"),
    [
        ("pytest -q", "pytest"),
        ("cd app && CI=1 python -m pytest", "python"),
        ("FOO=1 BAR=2 npm test", "npm"),
        ("/usr/bin/git status", "git"),
        ("C:\\tools\\Dotnet.exe test", "dotnet"),
        ("export A=1; make build", "make"),
        ("  ", None),
        (None, None),
        ("cd /tmp", None),
    ],
)
def test_the_first_program_skips_setup_and_assignments(
    command: str | None, program: str | None
) -> None:
    assert first_program(command) == program


def _call(
    event_id: int,
    command: str | None,
    *,
    failed: bool = False,
    tool: str = "Bash",
    stream: str = "main",
) -> ToolCall:
    return ToolCall(event_id, stream, tool, command, event_id + 1000, failed)


def test_a_failed_command_run_again_within_five_calls_is_a_retry() -> None:
    calls = [
        _call(1, "npm run build", failed=True),
        _call(2, None, tool="Edit"),
        _call(3, "ls"),
        _call(4, "npm run build"),
    ]
    [found] = find_command_retries(calls)
    assert (found.kind, found.corrected_by) == ("command_retry", "claude")
    assert (found.trigger_event_id, found.fix_event_id) == (1001, 4)


def test_the_retry_may_be_a_different_command_of_the_same_program() -> None:
    calls = [_call(1, "git push", failed=True), _call(2, "git push --force-with-lease")]
    assert len(find_command_retries(calls)) == 1


def test_a_sixth_call_is_too_late() -> None:
    calls = [_call(1, "make", failed=True)] + [_call(i, None, tool="Read") for i in range(2, 7)]
    calls.append(_call(7, "make"))
    assert find_command_retries(calls) == []


def test_other_programs_other_streams_and_successes_are_no_retries() -> None:
    assert find_command_retries([_call(1, "make", failed=True), _call(2, "ls")]) == []
    other_stream = [_call(1, "make", failed=True), _call(2, "make", stream="agent-a1")]
    assert find_command_retries(other_stream) == []
    assert find_command_retries([_call(1, "make"), _call(2, "make")]) == []


def test_each_failure_gets_its_own_retry() -> None:
    calls = [_call(1, "make", failed=True), _call(2, "make", failed=True), _call(3, "make")]
    found = find_command_retries(calls)
    assert [(f.trigger_event_id, f.fix_event_id) for f in found] == [(1001, 2), (1002, 3)]


def _edit(event_id: int, minute: float, start: int, lines: int, path: str = "a.py") -> Edit:
    hunk = {"old_start": start, "old_lines": lines, "new_start": start, "new_lines": lines}
    return Edit(event_id, path, START + timedelta(minutes=minute), [hunk])


def test_an_edit_of_lines_an_earlier_edit_wrote_is_rework() -> None:
    [found] = find_reworks([_edit(1, 0, 10, 5), _edit(2, 10, 12, 2)])
    assert (found.kind, found.corrected_by) == ("rework", "claude")
    assert (found.trigger_event_id, found.fix_event_id) == (1, 2)


def test_other_lines_other_files_and_old_edits_are_no_rework() -> None:
    assert find_reworks([_edit(1, 0, 10, 5), _edit(2, 5, 20, 2)]) == []
    assert find_reworks([_edit(1, 0, 10, 5), _edit(2, 5, 10, 5, path="b.py")]) == []
    assert find_reworks([_edit(1, 0, 10, 5), _edit(2, 31, 10, 5)]) == []
    assert len(find_reworks([_edit(1, 0, 10, 5), _edit(2, 30, 10, 5)])) == 1


def test_an_earlier_edit_counts_once_and_a_chain_counts_each_link() -> None:
    edits = [_edit(1, 0, 10, 5), _edit(2, 5, 10, 5), _edit(3, 6, 10, 5)]
    found = find_reworks(edits)
    assert [(f.trigger_event_id, f.fix_event_id) for f in found] == [(1, 2), (2, 3)]


def test_a_pure_insertion_next_to_nothing_does_not_overlap() -> None:
    insert = Edit(
        2,
        "a.py",
        START + timedelta(minutes=1),
        [{"old_start": 40, "old_lines": 0, "new_start": 41, "new_lines": 3}],
    )
    assert find_reworks([_edit(1, 0, 10, 5), insert]) == []
