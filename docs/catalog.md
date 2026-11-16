# The catalog: sessions you can read in MinIO

Levels: Claude account → project → session (one Claude Code session) → chat (everything said in it) →
conversations (one prompt and the answer to it). In MinIO: `catalog/accounts/<account>/projects/<project>/sessions/<session>/`.

The raw objects in the bucket (`sessions/<id>/<stream>/<offset>.jsonl`) are exact but hard to read: ids for
names and one JSON line per entry, split into chunks ([storage.md](storage.md)). The **catalog** is a readable
copy of the same chats under `catalog/` in the same bucket. It is derived and rebuildable: `sessions/` is never
touched, and deleting `catalog/` loses nothing.

## Layout

```text
catalog/
  README.md                       explains the layout (written by the tool)
  index.json                      accounts -> projects -> chats, each with its path
  accounts/<account>__<id8>/      the account label (else its name) and 8 characters of its id
    projects/<project>/           the project label (else its name)
      chats/<date>__<title>__<id8>/
        report.md                 the WHOLE chat in one readable file (see below)
        README.md                 what this chat is and what each file means
        00_summary.json           ids, account, project, title, start/end, models, tokens, cost, counts
        01_conversation.md        prompts, replies, tool calls (one line each), thinking notes
        02_sessions.json          each time the chat was opened
        03_decisions.json         the decisions: goal, options, chosen, reason, result, tests, files changed, who decided, who checked
        04_tests.json             test runs with their test cases
        05_code_changes.json      file changes with role, and the commits linked to the chat
        06_corrections.json       what had to be put right
        07_subagents_and_workflows.json   sub-agents, workflow runs, slash commands
        08_usage.json             tokens, effort and cost per model
        transcript/main.json      the raw lines of the chat, as a JSON array (indent 2)
        transcript/agents/<agent type>__<agent id8>.json   the same for each sub-agent
```

* Names are slugs: lowercase `a-z`, `0-9` and dashes. The title part is at most 60 characters.
* `<date>` is the start of the chat in UTC. A chat without an account is filed under
  `unknown-account__00000000`, one without a project under `unknown-project`.
* The id at the end of a chat folder (`<id8>`, the first 8 characters of the chat id) never changes. When a title
  or a label changes, the chat is written to its new folder and the old folder is removed.
* Chats without a message (`session.is_empty`) are not listed; an existing folder of such a chat is removed.
* Summary files (`00_summary.json`) have sorted keys; the others keep a fixed, documented order. All JSON is UTF-8
  with real characters (no `\u` escapes). The raw lines in `transcript/` are the lines as Claude Code wrote them:
  a large attachment value that capture moved into `attachments/` is put back (if it is missing, the pointer
  stays), and a line that cannot be read becomes `{"type": "ccp-unreadable-line", ...}` so that the array always
  has one item per original line.
* References such as `{"key": "sessions/<id>/main/000000000000.jsonl", "line": 12}` in the JSON files point to a
  line of a raw object.
* No file contains an e-mail address: an account is named by its label or display name. The conversation and
  transcripts contain the (already redacted) text of the chat, so give access to the bucket as you would to the
  raw data.

## Commands

| Task | Command |
| --- | --- |
| Write one chat (and the index) | `python -m ccp.catalog build --chat <chat id>` |
| Write every chat, remove empty chats, write the index | `python -m ccp.catalog build --all` |
| One chat as a single Markdown file (printed, or `--out file.md`) | `python -m ccp.catalog export --chat <id> [--out file.md]` |
| Print the catalog as a tree | `python -m ccp.catalog show` (add `--files` to list each file) |

The same works as `ccp-catalog` once the command is registered in `pyproject.toml`. `build` reads the index in
PostgreSQL and the raw objects, and writes only under `catalog/`. It reports a chat it cannot write (for example
because a raw object is missing) and carries on with the others; the exit code is then 1.

From code: `catalog_chat(connection, store, session_id)` returns the chat's folder (or `None` for an empty chat),
and `catalog_all(connection, store)` returns what it wrote, removed and could not write. Both can be called after
the indexer has refreshed a chat, so the catalog stays current.

## How it is made

`ccp.catalog.loader` reads the facts of a chat from the tables and views of the index; `conversation` and
`documents` turn them into the files; `transcripts` reads the raw chunks back (in order, one item per line);
`builder` writes the files, deletes files that no longer belong, and finds a chat's old folder by its `<id8>`
(checking the `chat_id` in its `00_summary.json`, so two chats that start with the same 8 characters are never
mixed up). Writing a chat twice gives identical objects.

Tool results kept by Claude Code in files (`sessions/<id>/tool-results/`) are not copied into the catalog.

## The single-file report (`report.md`)

Every chat folder has `report.md`: the content of all the other files of the folder in one readable
Markdown file, with no JSON. It is also made on demand by `ccp-catalog export` and by the **Download the
whole chat as one .md file** link on each Explorer chat page (same access rule as the page; logged as
`export_chat`). Code: [`report.py`](../src/ccp/catalog/report.py).

Sections: 1 Summary (with tokens and counts), 2 Openings of the session, 3 Conversation (one *turn* per
developer prompt: prompt, thinking, replies, every tool call with its full input and output in collapsible
blocks, and the files changed and test runs of that turn), 4 Decisions (goal, options, chosen, reason, result,
tests, who decided, which model wrote it, who reviewed it, files, thinking notes, evidence), 5 Code changes
and linked commits, 6 Tests, 7 Corrections, 8 Sub-agents, workflows and slash commands (instructions and
results in full), 9 Usage and cost (a model that only appears in Claude Code's cost record is marked
"internal helper"), 10 About this report. A section without data says *None recorded*. Claude's own
headings are pushed down so they never outrank the turn they belong to. Only the raw transcript is not
copied; the report gives its object keys and line numbers.

A test (`tests/unit/catalog/test_catalog_report.py`) walks every value of the JSON documents and fails if
one is missing from the report.

### When `report.md` is refreshed (nothing to run by hand)

`ccp-server` rewrites a chat's folder, `report.md` included, **after every turn**: each reply uploads the new
transcript lines, the indexer processes them, and the catalog step runs right after. That step is *light*: it
rewrites the report and the readable files but does not copy the raw transcript again (the copy in
`transcript/` is refreshed by a full build). A **full** build runs when the decisions of the chat are written
(at the end of a session, or after quiet time) and with `ccp-catalog build`. A person's approval or rejection of
a decision in the Explorer queues the chat for a refresh too. The report also lists the capture commits of the
chat (who made each change). The server process must run the current code: restart `ccp-server` after an
upgrade, or the old process keeps writing the old files.

At server start the whole catalog is written again with the current code, so an upgrade never leaves a session
without `report.md`. The report contains the main chat and every sub-agent's conversation in full, with every shell
command and its output.
