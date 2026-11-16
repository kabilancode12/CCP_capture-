# MCP: Claude asks "was this tried before?"

`ccp-mcp` is a small [MCP](https://modelcontextprotocol.io) server over stdio (no extra packages). Register it:

```bash
claude mcp add ccp-approaches -- python -m ccp.mcp
```

It needs the same `POSTGRES_*` settings as the other commands and is read-only. Tools:

| Tool | Gives |
| --- | --- |
| `search_approaches(query, only_removed?)` | approaches whose text contains every word, with state and what replaced them |
| `get_approach(id)` | one approach in full: goal, options, choice, reason, result, tests, files, thinking, what became of it |

Only decision summaries are returned, never chat transcripts. Code: [`server.py`](../src/ccp/mcp/server.py).
