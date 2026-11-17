"""Reading a sub-agent's description."""

import json

import pytest

from ccp.index.agents import (
    AgentMeta,
    AgentMetaError,
    agent_id_of,
    is_meta_stream,
    parse_agent_meta,
)


def test_a_description_is_read() -> None:
    line = json.dumps(
        {
            "agentType": "Explore",
            "description": "Find the config",
            "toolUseId": "toolu_1",
            "spawnDepth": 2,
            "model": "model-x",
            "requestShape": "something new",
        }
    )
    assert parse_agent_meta(line) == AgentMeta(
        "Explore", "Find the config", "model-x", 2, "toolu_1"
    )


def test_missing_fields_are_empty() -> None:
    assert parse_agent_meta("{}") == AgentMeta(None, None, None, None, None)


@pytest.mark.parametrize("depth", ["2", 2.5, True, None, [1]])
def test_a_spawn_depth_that_is_not_a_whole_number_is_ignored(depth: object) -> None:
    assert parse_agent_meta(json.dumps({"spawnDepth": depth})).spawn_depth is None


def test_wrong_types_and_empty_strings_are_empty() -> None:
    meta = parse_agent_meta(json.dumps({"agentType": 5, "description": "", "toolUseId": ["x"]}))
    assert meta == AgentMeta(None, None, None, None, None)


def test_null_characters_are_replaced() -> None:
    assert parse_agent_meta(json.dumps({"description": "a\x00b"})).description == "a\ufffdb"


@pytest.mark.parametrize("line", ["", "nope", "[1]", "3", "null"])
def test_something_that_is_not_an_object_is_an_error(line: str) -> None:
    with pytest.raises(AgentMetaError):
        parse_agent_meta(line)


@pytest.mark.parametrize(
    ("stream", "is_meta", "agent_id"),
    [
        ("agent-a1.meta", True, "a1"),
        ("agent-a1", False, "a1"),
        ("main", False, "main"),
        ("agent-a1.meta.old", False, "a1.meta.old"),
        ("other.meta", False, "other"),
    ],
)
def test_streams_are_told_apart(stream: str, is_meta: bool, agent_id: str) -> None:
    assert is_meta_stream(stream) is is_meta
    assert agent_id_of(stream) == agent_id


def test_the_model_is_read_from_picked_effort_model_as_real_files_write_it() -> None:
    line = json.dumps({"agentType": "Explore", "pickedEffortModel": "model-y"})
    assert parse_agent_meta(line).model == "model-y"


def test_model_is_preferred_over_picked_effort_model() -> None:
    line = json.dumps({"model": "model-x", "pickedEffortModel": "model-y"})
    assert parse_agent_meta(line).model == "model-x"


@pytest.mark.parametrize("model", ["", None, 5])
def test_an_unusable_model_falls_back_to_picked_effort_model(model: object) -> None:
    line = json.dumps({"model": model, "pickedEffortModel": "model-y"})
    assert parse_agent_meta(line).model == "model-y"


def test_effort_and_the_workflow_run_are_read_from_the_description() -> None:
    line = json.dumps(
        {
            "agentType": "workflow-subagent",
            "effort": "high",
            "ccpWorkflowRunId": "wf_1",
            "workflowName": "build-stage",
        }
    )
    meta = parse_agent_meta(line)
    assert (meta.effort, meta.workflow_run_id, meta.workflow_name) == (
        "high",
        "wf_1",
        "build-stage",
    )


def test_a_description_without_them_leaves_them_empty() -> None:
    meta = parse_agent_meta('{"agentType": "Explore"}')
    assert (meta.effort, meta.workflow_run_id, meta.workflow_name) == (None, None, None)
