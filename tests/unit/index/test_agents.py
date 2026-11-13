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
