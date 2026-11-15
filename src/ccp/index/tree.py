"""The conversation tree: which entries belong to a branch that was abandoned.

Every transcript entry names its parent (``parentUuid``), so a session is a tree, not a list. When a
developer **rewinds** in Claude Code and types a new prompt, the old prompt and everything after it
stay in the file, and the new prompt becomes a *second* user prompt under the same parent entry
(observed, fact 19 in docs/verify/transcript-facts.md). The older branch was abandoned: it is real
history (it was captured, and AI edits on it may have happened), but it is not the conversation
that continued.

Rule: when an entry has two or more user prompts as children, the **last** one in the file is the
one that continued. Every earlier one, and everything below it, is abandoned. Tool calls also give
an entry two children, so only user prompts count. A slash command the developer typed is a user
prompt; messages that Claude Code injects itself (``isMeta``), command output, messages from other
agents or background tasks, and sub-agent prompts are not.

A compaction cuts the chain: the boundary entry has no parent but names ``logicalParentUuid``
(fact 20). That link is followed as if it were the parent, so a branch that contains a compaction is
abandoned as a whole.

The module is pure: nodes in, ids out. Reading and writing the database is in ``repository.py``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
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


def nearest_prompts(
    nodes: Iterable[TreeNode], starts: Iterable[int], launch_of_agent: Mapping[str, int]
) -> dict[int, int | None]:
    """For each start event, find the developer prompt that led to it.

    Walks up the tree from the event to the nearest user prompt. A sub-agent's chain ends at its
    own first entry, which has no parent: the walk then jumps to the call that launched the agent
    (``launch_of_agent`` maps an agent id to that event) and carries on, so an edit made by a
    sub-agent is traced back to the prompt that caused the sub-agent.

    Args:
        nodes: Every event of one session.
        starts: The event ids to trace.
        launch_of_agent: Agent id to the id of the event that launched it.

    Returns:
        Start event id to the prompt's event id, or None if no prompt is found.
    """
    by_id = {node.event_id: node for node in nodes}
    by_uuid: dict[str, TreeNode] = {}
    for known in sorted(by_id.values(), key=lambda n: n.position):
        if known.entry_uuid is not None:
            by_uuid.setdefault(known.entry_uuid, known)  # a repeated uuid: the first entry counts

    found: dict[int, int | None] = {}
    for start in starts:
        node: TreeNode | None = by_id.get(start)
        seen: set[int] = set()
        while node is not None and node.event_id not in seen and not node.is_user_prompt:
            seen.add(node.event_id)
            parent = node.parent_uuid or node.logical_parent_uuid
            if parent is not None:
                node = by_uuid.get(parent)
                continue
            stream = node.position[0].split("/")[2] if node.position[0].count("/") >= 3 else ""
            launch = (
                launch_of_agent.get(stream.removeprefix("agent-"))
                if stream.startswith("agent-")
                else None
            )
            node = by_id.get(launch) if launch is not None else None
        found[start] = node.event_id if node is not None and node.is_user_prompt else None
    return found
