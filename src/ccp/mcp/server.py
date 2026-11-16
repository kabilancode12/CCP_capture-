"""The CCP approach library as an MCP server (stdio, JSON-RPC, no extra packages).

Register it with Claude Code::

    claude mcp add ccp-approaches -- python -m ccp.mcp

Claude then has two read-only tools:

* ``search_approaches(query, only_removed?)``: approaches (decisions with their code) whose text
  contains every word, with their state (active, replaced, removed), why, and what replaced them;
* ``get_approach(id)``: one approach in full: goal, options, choice, reason, result, tests, files,
  thinking at the time, and what became of it.

Use it at the start of a task to find out whether something similar was tried, kept or dropped,
and why. Nothing is written. Only decision summaries are returned, never chat transcripts.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from ccp.approaches import library
from ccp.approaches.cli import describe, line
from ccp.db.connection import connect
from ccp.settings import SettingsError, load_database_settings

PROTOCOL_VERSION = "2024-11-05"
TOOLS = [
    {
        "name": "search_approaches",
        "description": (
            "Search the team's approach library: past decisions with their code, including ones "
            "that were replaced or removed and never committed. Every word must appear."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to look for"},
                "only_removed": {"type": "boolean", "description": "only replaced or removed"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_approach",
        "description": "One approach in full: why chosen, options, result, what became of it.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Run one tool and return its text answer."""
    connection = connect(load_database_settings())
    try:
        if name == "search_approaches":
            found = library.search(
                connection,
                str(arguments.get("query", "")),
                only_removed=bool(arguments.get("only_removed")),
                limit=15,
            )
            return "\n".join(map(line, found)) if found else "No approach matches."
        if name == "get_approach":
            item = library.get(connection, int(arguments["id"]))
            return "\n".join(describe(item)) if item else "There is no such approach."
        raise ValueError(f"unknown tool {name}")
    finally:
        connection.close()


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message (``None`` for a notification)."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    result: dict[str, Any]
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ccp-approaches", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        try:
            text, failed = call_tool(str(params.get("name")), params.get("arguments") or {}), False
        except (SettingsError, ValueError, KeyError, OSError) as error:
            text, failed = f"The approach library is not available: {type(error).__name__}", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": ident,
            "error": {"code": -32601, "message": "unknown method"},
        }
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def main() -> int:
    """Read one JSON message per line from standard input; answer on standard output."""
    for text in sys.stdin:
        try:
            message = json.loads(text)
            reply = handle(message) if isinstance(message, dict) else None
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "bad JSON"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0
