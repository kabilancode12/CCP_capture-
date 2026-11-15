"""Sub-agent descriptions.

Claude Code writes a small ``agent-<id>.meta.json`` next to each sub-agent transcript (fact 17 in
docs/verify/transcript-facts.md). The capture client uploads it as the one-line stream
``agent-<id>.meta``. It says what kind of agent it is and, in ``toolUseId``, which call of the
parent launched it. This module reads it; the database part is in ``repository.py``.

Like the transcript parser, it never refuses a file because of fields it does not know: new ones
will appear. A field that is missing or has the wrong type is simply left empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

META_SUFFIX = ".meta"


class AgentMetaError(ValueError):
    """Raised for a description that is not a JSON object. The message never contains it."""


@dataclass(frozen=True)
class AgentMeta:
    """What the indexer keeps of a sub-agent's description."""

    agent_type: str | None
    description: str | None
    model: str | None
    spawn_depth: int | None  # 1 = launched by the main conversation
    tool_use_id: str | None  # the id of the parent's tool call that launched this agent
    effort: str | None = None  # reasoning effort, if the description names it
    workflow_run_id: str | None = None  # the Workflow run that started it (added by the client)
    workflow_name: str | None = None


def is_meta_stream(stream: str) -> bool:
    """True for the stream that holds a sub-agent's description (``agent-<id>.meta``)."""
    return stream.startswith("agent-") and stream.endswith(META_SUFFIX)


def agent_id_of(stream: str) -> str:
    """The agent id in a stream name: ``agent-a1`` and ``agent-a1.meta`` both give ``a1``."""
    return stream.removeprefix("agent-").removesuffix(META_SUFFIX)


def parse_agent_meta(line: str) -> AgentMeta:
    """Read a sub-agent description.

    Args:
        line: The uploaded line (the content of the ``.meta.json`` file).

    Raises:
        AgentMetaError: If it is not valid JSON or not a JSON object.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as error:
        raise AgentMetaError("description is not valid JSON") from error
    if not isinstance(data, dict):
        raise AgentMetaError("description is not a JSON object")
    depth = data.get("spawnDepth")
    return AgentMeta(
        agent_type=_text(data.get("agentType")),
        description=_text(data.get("description")),
        # Real description files name the model in "pickedEffortModel"; "model" is read first in
        # case a version writes that.
        model=_text(data.get("model")) or _text(data.get("pickedEffortModel")),
        spawn_depth=depth if isinstance(depth, int) and not isinstance(depth, bool) else None,
        tool_use_id=_text(data.get("toolUseId")),
        # The client adds the run id of a Workflow sub-agent as "ccpWorkflowRunId" (the name of the
        # folder subagents/workflows/<run id>/, which the description file itself does not hold).
        effort=_text(data.get("effort")) or _text(data.get("pickedEffort")),
        workflow_run_id=_text(data.get("ccpWorkflowRunId")) or _text(data.get("workflowRunId")),
        workflow_name=_text(data.get("workflowName")),
    )


def _text(value: object) -> str | None:
    """A non-empty string with NUL characters replaced (PostgreSQL cannot store them), or None."""
    return value.replace("\x00", "�") if isinstance(value, str) and value else None
