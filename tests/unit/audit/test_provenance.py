"""Replaying edits to find which one wrote a line."""

from ccp.audit.provenance import Edit, Hunk, owner_of, replay


def _hunk(old_start: int, old_lines: int, *lines: str) -> Hunk:
    return Hunk(old_start, old_lines, lines)


def _file(change_id: int, count: int) -> Edit:
    """An edit that wrote a whole new file of ``count`` lines."""
    return Edit(change_id, created_lines=count)


def test_a_new_file_is_owned_by_the_edit_that_wrote_it() -> None:
    assert replay([_file(1, 3)]) == [1, 1, 1]


def test_no_edits_means_no_owners() -> None:
    assert replay([]) == []
    assert owner_of([], 1) is None


def test_a_replaced_line_changes_owner_and_its_neighbours_do_not() -> None:
    owners = replay([_file(1, 5), Edit(2, (_hunk(2, 3, " b", "-c", "+C", " d"),))])
    assert owners == [1, 1, 2, 1, 1]


def test_an_insertion_shifts_the_lines_below_it() -> None:
    owners = replay([_file(1, 3), Edit(2, (_hunk(2, 1, " b", "+new1", "+new2"),))])
    assert owners == [1, 1, 2, 2, 1]


def test_a_deletion_shifts_the_lines_below_it_up() -> None:
    owners = replay([_file(1, 4), Edit(2, (_hunk(2, 3, " b", "-c", " d"),))])
    assert owners == [1, 1, 1]


def test_a_later_edit_overwrites_an_earlier_one() -> None:
    edits = [
        _file(1, 3),
        Edit(2, (_hunk(2, 1, "-b", "+B"),)),
        Edit(3, (_hunk(2, 1, "-B", "+BB"),)),
    ]
    assert replay(edits) == [1, 3, 1]


def test_lines_that_move_keep_their_owner() -> None:
    edits = [
        _file(1, 3),
        Edit(2, (_hunk(1, 0, "+top"),)),  # inserted before line 1 (start 1 with no old lines)
    ]
    # A zero-length hunk's start is the line BEFORE the insertion: start 1 means after line 1.
    assert replay(edits) == [1, 2, 1, 1]


def test_an_insertion_at_the_very_start_uses_start_zero() -> None:
    edits = [_file(1, 2), Edit(2, (_hunk(0, 0, "+first"),))]
    assert replay(edits) == [2, 1, 1]


def test_several_hunks_of_one_patch_are_in_the_old_files_coordinates() -> None:
    patch = (
        _hunk(2, 1, "-b", "+b1", "+b2"),  # grows the file by one line
        _hunk(5, 1, "-e", "+E"),  # refers to line 5 of the OLD file
    )
    owners = replay([_file(1, 6), Edit(2, patch)])
    assert owners == [1, 2, 2, 1, 1, 2, 1]


def test_hunks_are_applied_whatever_order_they_are_listed_in() -> None:
    patch = (_hunk(5, 1, "-e", "+E"), _hunk(2, 1, "-b", "+b1", "+b2"))
    assert replay([_file(1, 6), Edit(2, patch)]) == [1, 2, 2, 1, 1, 2, 1]


def test_an_edit_of_a_file_whose_start_is_unknown_leaves_unknown_lines_without_owner() -> None:
    owners = replay([Edit(1, (_hunk(10, 1, " ctx", "+added"),))])
    assert owners[:9] == [None] * 9
    assert owners[9:] == [None, 1]


def test_an_edit_that_runs_past_the_known_end_does_not_fail() -> None:
    assert replay([_file(1, 1), Edit(2, (_hunk(1, 3, " a", " b", " c", "+d"),))]) == [
        1,
        None,
        None,
        2,
    ]


def test_lines_that_are_not_file_lines_are_ignored() -> None:
    owners = replay(
        [_file(1, 2), Edit(2, (_hunk(1, 1, "-a", "+A", "\\ No newline at end of file"),))]
    )
    assert owners == [2, 1]


def test_writing_a_file_again_starts_over() -> None:
    assert replay([_file(1, 3), Edit(2, (_hunk(1, 1, "-a", "+A"),)), _file(3, 2)]) == [3, 3]


def test_a_patch_after_a_rewrite_applies_to_the_new_file() -> None:
    assert replay([_file(1, 3), _file(2, 3), Edit(3, (_hunk(3, 1, "-c", "+C"),))]) == [2, 2, 3]


def test_owner_of_a_line_outside_the_known_file_is_none() -> None:
    owners = [1, 2]
    assert [owner_of(owners, n) for n in (0, 1, 2, 3, -1)] == [None, 1, 2, None, None]
