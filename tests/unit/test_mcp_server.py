"""The MCP server speaks the protocol (the library itself is tested with a real database)."""

from typing import Any

import pytest

from ccp.mcp import server


def test_the_handshake_lists_both_tools() -> None:
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init is not None and init["result"]["serverInfo"]["name"] == "ccp-approaches"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools is not None
    assert [t["name"] for t in tools["result"]["tools"]] == ["search_approaches", "get_approach"]


def test_notifications_get_no_answer_and_unknown_methods_an_error() -> None:
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert reply is not None and reply["error"]["code"] == -32601


def test_a_tool_call_returns_text_and_reports_a_missing_database_as_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(name: str, arguments: dict[str, Any]) -> str:
        return f"{name}:{arguments['query']}"

    monkeypatch.setattr(server, "call_tool", fake)
    ok = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "search_approaches", "arguments": {"query": "retry"}}})  # fmt: skip
    assert ok is not None and ok["result"]["content"][0]["text"] == "search_approaches:retry"
    assert ok["result"]["isError"] is False

    def broken(name: str, arguments: dict[str, Any]) -> str:
        raise OSError("database down")

    monkeypatch.setattr(server, "call_tool", broken)
    bad = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "search_approaches", "arguments": {}}})  # fmt: skip
    assert bad is not None and bad["result"]["isError"] is True
