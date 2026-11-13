"""The conversation tree: which entries belong to a branch that was abandoned.

Every transcript entry names its parent (``parentUuid``), so a session is a tree, not a list. When a
developer **rewinds** in Claude Code and types a new prompt, the old prompt and everything after it
stay in the file, and the new prompt becomes a *second* user prompt under the same parent entry
(observed, fact 19 in docs/verify/transcript-facts.md). The older branch was abandoned: it is real
history (it was captured, and AI edits on it may have happened), but it is not the conversation
that continued.

Rule: when an entry has two or more user prompts as children, the **last** one in the file is the
one that continued. Every earlier one, and everything below it, is abandoned. Tool calls also give
an entry two children, so only user prompts count. Messages that Claude Code injects itself
(``isMeta``) and sub-agent prompts are not user prompts.

A compaction cuts the chain: the boundary entry has no parent but names ``logicalParentUuid``
(fact 20). That link is followed as if it were the parent, so a branch that contains a compaction is
abandoned as a whole.

The module is pure: nodes in, ids out. Reading and writing the database is in ``repository.py``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class TreeNode:
    """What the tree rule needs to know about one event."""

    event_id: int
    entry_uuid: str | None
    parent_uuid: str | None  # the entry's own parent
    logical_parent_uuid: str | None  # set on a compaction boundary, which has no parent
    is_user_prompt: bool  # a prompt written by the developer (not injected, not a sub-agent's)
    position: tuple[str, int]  # (object key, line number): the order in the file


def abandoned_event_ids(nodes: Iterable[TreeNode]) -> set[int]:
    """Find the events that belong to abandoned branches.

    Args:
        nodes: Every event of one session (any order).

    Returns:
        The ids of the events to mark as abandoned. Empty when nothing was rewound.
    """
    nodes = list(nodes)
    children: dict[str, list[TreeNode]] = defaultdict(list)
    for node in nodes:
        parent = node.parent_uuid or node.logical_parent_uuid
        if parent is not None:
            children[parent].append(node)

    abandoned: set[int] = set()
    for siblings in children.values():
        prompts = sorted((n for n in siblings if n.is_user_prompt), key=lambda n: n.position)
        for older in prompts[:-1]:  # the last prompt is the one that continued
            _mark_branch(older, children, abandoned)
    return abandoned


def _mark_branch(root: TreeNode, children: dict[str, list[TreeNode]], marked: set[int]) -> None:
    """Mark ``root`` and everything below it. Iterative, so a very long branch cannot overflow."""
    pending = [root]
    while pending:
        node = pending.pop()
        if node.event_id in marked:
            continue
        marked.add(node.event_id)
        if node.entry_uuid is not None:
            pending.extend(children.get(node.entry_uuid, ()))
