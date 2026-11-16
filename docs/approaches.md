# The approach library

An **approach** is a decision together with its code. The capture chain ([snapshots.md](snapshots.md)) keeps
the code of every step, even code that was never committed or was later deleted, so a team can find what
was tried before, see why it was chosen and why it was dropped, and bring it back. It works for any kind of
approach: an implementation, a pattern, a configuration, a way of structuring a module. Nothing here is
specific to one kind of code.

## How the code is found

`ccp-approaches refresh` (and the server's decision sweep) works out, for each decision, from recorded facts
only ([`lineage.py`](../src/ccp/approaches/lineage.py)):

| Field | Rule |
| --- | --- |
| `code_sha` | the newest capture commit of the decision's own chat that added or changed one of the files the decision lists |
| `removed_in_sha`, `removed_at` | the first later capture commit of the same project that deleted a file the decision created |
| `removed_by_decision_id` | the decision, in the chat of that commit, that lists the file as deleted; it **supersedes** the removed one |
| `lifecycle` | `active`, `superseded` (removed and replaced by a decision), or `removed` |

File names in decisions are relative to the chat folder and in commits relative to the repository root; two
names match when one ends with the other on a folder boundary. A decision that lists no files has no code.

## Commands

| Task | Command |
| --- | --- |
| Recompute the lineage | `ccp-approaches refresh` |
| Find approaches (every word must appear) | `ccp-approaches search retry backoff` (`--removed`: only replaced or removed) |
| One approach in full | `ccp-approaches show 42` |
| Two side by side, with their code difference | `ccp-approaches compare 42 57 --repo <capture clone>` |
| Bring the files back | `ccp-approaches restore 42 --repo <capture clone> --into <folder> --by <you> --purpose "<what for>"` |

`restore` writes the files as they were in the approach's code commit (it refuses to overwrite unless
`--force`) and records the reuse in `approach_reuse`, so the library shows how often an approach came back.
`<capture clone>` is a clone of the capture project (`git clone https://.../<repo>-capture.git`).

## In the Explorer and for Claude

The Explorer has **Approaches** (search, open, compare), **Timeline**, **Review**, **Activity**, **Insights**
and **Alerts** pages, and a **Changes by cause** tab on each chat ([explorer.md](explorer.md)). The MCP server
([mcp.md](mcp.md)) lets Claude itself ask "was this tried before?".

## Limits

- Only files a decision lists are followed; a decision without files has no code. Files written by a shell
  command are listed when the command is simple (see [decisions.md](decisions.md)).
- "Removed" means a capture commit deleted the file. Code that was edited away without deleting the file is
  still `active`.
- Approaches are only as good as the decisions: they are written by a model and reviewed by another, and a
  person can override them.
