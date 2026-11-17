"""The conversation-tree rule: which entries belong to an abandoned branch."""

from ccp.index.tree import TreeNode, abandoned_event_ids


def _node(
    event_id: int,
    uuid: str,
    parent: str | None,
    *,
    prompt: bool = False,
    logical_parent: str | None = None,
) -> TreeNode:
    """An event whose position in the file is its id."""
    return TreeNode(event_id, uuid, parent, logical_parent, prompt, ("main/0", event_id))


def test_a_straight_conversation_has_no_abandoned_events() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "a1", "p1"),
        _node(3, "p2", "a1", prompt=True),
        _node(4, "a2", "p2"),
    ]
    assert abandoned_event_ids(nodes) == set()


def test_no_events_means_nothing_abandoned() -> None:
    assert abandoned_event_ids([]) == set()


def test_a_rewind_abandons_the_older_prompt_and_everything_below_it() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "a1", "p1"),
        _node(3, "p2", "a1", prompt=True),  # the old branch
        _node(4, "a2", "p2"),
        _node(5, "a3", "a2"),
        _node(6, "p3", "a1", prompt=True),  # after the rewind: this one continued
        _node(7, "a4", "p3"),
    ]
    assert abandoned_event_ids(nodes) == {3, 4, 5}


def test_the_last_prompt_in_the_file_wins_whatever_order_the_nodes_come_in() -> None:
    nodes = [
        _node(7, "a4", "p3"),
        _node(6, "p3", "a1", prompt=True),
        _node(3, "p2", "a1", prompt=True),
        _node(1, "p1", None, prompt=True),
        _node(2, "a1", "p1"),
    ]
    assert abandoned_event_ids(nodes) == {3}


def test_three_prompts_under_one_parent_abandon_the_first_two() -> None:
    nodes = [
        _node(1, "a1", None),
        _node(2, "p1", "a1", prompt=True),
        _node(3, "p2", "a1", prompt=True),
        _node(4, "p3", "a1", prompt=True),
    ]
    assert abandoned_event_ids(nodes) == {2, 3}


def test_a_second_rewind_inside_an_abandoned_branch_changes_nothing_extra() -> None:
    nodes = [
        _node(1, "a1", None),
        _node(2, "p1", "a1", prompt=True),
        _node(3, "a2", "p1"),
        _node(4, "p2", "a2", prompt=True),
        _node(5, "p3", "a2", prompt=True),
        _node(6, "p4", "a1", prompt=True),
    ]
    assert abandoned_event_ids(nodes) == {2, 3, 4, 5}


def test_two_tool_calls_under_one_entry_are_not_a_rewind() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "t1", "p1"),
        _node(3, "t2", "p1"),
    ]
    assert abandoned_event_ids(nodes) == set()


def test_a_prompt_that_is_not_the_developers_does_not_count() -> None:
    nodes = [
        _node(1, "a1", None),
        _node(2, "p1", "a1", prompt=True),
        _node(3, "meta", "a1", prompt=False),  # injected by Claude Code, or a sub-agent's
    ]
    assert abandoned_event_ids(nodes) == set()


def test_a_compaction_is_followed_through_its_logical_parent() -> None:
    nodes = [
        _node(1, "a1", None),
        _node(2, "p1", "a1", prompt=True),  # abandoned branch, which contains a compaction
        _node(3, "b1", None, logical_parent="p1"),
        _node(4, "s1", "b1"),
        _node(5, "p2", "a1", prompt=True),
    ]
    assert abandoned_event_ids(nodes) == {2, 3, 4}


def test_a_compaction_alone_abandons_nothing() -> None:
    nodes = [
        _node(1, "p1", None, prompt=True),
        _node(2, "a1", "p1"),
        _node(3, "b1", None, logical_parent="a1"),
        _node(4, "s1", "b1"),
        _node(5, "p2", "s1", prompt=True),
    ]
    assert abandoned_event_ids(nodes) == set()


def test_a_repeated_uuid_that_loops_back_cannot_hang_the_walk() -> None:
    nodes = [
        _node(1, "a1", None),
        _node(2, "p1", "a1", prompt=True),  # abandoned
        _node(3, "c1", "p1"),
        _node(4, "p1", "c1"),  # a second entry reusing the uuid "p1": the walk would loop
        _node(5, "p2", "a1", prompt=True),
    ]
    assert abandoned_event_ids(nodes) == {2, 3, 4}
