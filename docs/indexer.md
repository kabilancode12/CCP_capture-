# The indexer

The indexer turns the transcripts stored in MinIO into rows in PostgreSQL. It runs on the server only, as a
separate process from the ingestion API. The code is in [`src/ccp/index/`](../src/ccp/index/).

The index is **derived data**: everything in it comes from the stored transcripts. It can be deleted and
rebuilt at any time, and indexing the same transcript twice gives the same rows.

## Running it

Apply the migrations first (`ccp-db migrate`; the indexer refuses to start on an old schema), then:

| Task | Command |
| --- | --- |
| Index continuously (wait for new jobs) | `ccp-index run` |
| Index what is queued, then exit | `ccp-index run --once` |

Settings are the `POSTGRES_*` and `MINIO_*` variables ([configuration.md](configuration.md)). If the launcher
is blocked on your machine, use `python -m ccp.index run`. `--poll-seconds N` sets how long it waits when the
queue is empty (default 5).

## How a session is indexed

The ingestion API queues one `index_session` job per session when chunks arrive, and once more when the
session is closed. A job means "bring this session's index up to date":

1. List the session's stored chunks: the main transcript and any sub-agent streams.
2. Skip chunks that are already indexed. A chunk never changes: its key holds its position in the source file.
3. Read each new chunk and store **one `event` row per transcript line**, in one transaction per chunk.
4. Recompute what is derived from all of the session's events (see below), in one transaction. This runs at
   the end of every job, even when no chunk was new, so it is always current.

Because of (2) and (3), a crash leaves a chunk fully indexed or not at all, a repeated job adds nothing, and
a chunk that arrives later is simply added. A failed job is tried again up to three times, then stays
`failed` (see `job.status`); a new chunk of the session queues a fresh job.

Sub-agent entries are stored under the **parent's** session id, with the actor `subagent`. The stream's
name is in the event's `raw_object_key` (`sessions/<id>/agent-<agent id>/...`).

## What an entry becomes

| Transcript entry | `event_type` | `actor` |
| --- | --- | --- |
| user, a plain prompt | `prompt` | `human` (`subagent` inside a sub-agent) |
| user, tool results | `tool_result` | `system` |
| user, compaction summary | `compaction_summary` | `system` |
| assistant, with a tool call | `tool_call` | `ai` (`subagent` inside a sub-agent) |
| assistant, with text | `response` | `ai` (`subagent` inside a sub-agent) |
| assistant, only thinking | `thinking` | `ai` (`subagent` inside a sub-agent) |
| system | its `subtype`, for example `compact_boundary` | `system` |
| any other type | the type itself, for example `cost-state`, or `unknown` | `system` |

Unknown types are stored, not rejected: Claude Code adds new ones. Each event keeps the whole entry in
`payload` (already redacted on the developer machine), the entry's `uuid` and `parentUuid`, its time, and where
it came from (`raw_object_key`, `raw_line_number`; the line number counts from 1 within the chunk). A NUL
character, which PostgreSQL cannot store, is replaced by U+FFFD.

## Abandoned branches (rewind)

Entries form a tree through `parentUuid`. When the developer rewinds and types a new prompt, the old prompt and
everything after it stay in the transcript, and the new prompt becomes a second user prompt under the same
parent ([facts 19 and 20](verify/transcript-facts.md)). The rule: **when an entry has two or more user prompts
as children, the last one in the file continued; every earlier one, and everything below it, is abandoned**
(`event.is_abandoned = true`). Abandoned events are kept: they were really captured, and AI edits on them may
have happened. Only user prompts written by a person count: tool calls (which also give an entry two
children), messages Claude Code injects itself (`isMeta`) and sub-agent prompts do not. A compaction boundary
has no parent but names `logicalParentUuid`, which is followed as if it were the parent. The code is
[`tree.py`](../src/ccp/index/tree.py).

## Compactions

Every `compact_boundary` entry gets a row in `compaction`: the trigger (`manual` or `auto`), the token counts
before and after, and the text of the summary entry that follows it. The summary may arrive in a later chunk
than the boundary; the row is completed then. A boundary without metadata still gets a row, with empty fields.

## Session facts

From the main transcript (not sub-agent streams) the indexer fills `session.started_at` (the earliest entry
time) and `session.branch` (the first Git branch named by an entry). A value already stored is kept if the
transcript says nothing. `ended_at` and `end_reason` are not set here: the idle-session closer and the
SessionEnd hook set them. `repository` and `start_commit` are not in the transcript (it has only the local
folder) and stay empty until the capture client sends them.

## Sub-agent runs

Each sub-agent has its own transcript (stream `agent-<id>`) and a one-line description (stream
`agent-<id>.meta`, fact 17). The description is **not** an event: it is read into `agent_run` instead. One row
per run, with:

| Column | From |
| --- | --- |
| `agent_type`, `description`, `model`, `spawn_depth`, `tool_use_id` | the description |
| `launch_event_id` | the event whose tool call has that `tool_use_id`: the parent's `Agent` call, or a call inside another sub-agent for nested agents |
| `instructions` | the `prompt` of that call |
| `result` | the text of the sub-agent's last plain reply (empty if it ended on a tool call) |
| `started_at`, `ended_at`, `event_count` | the sub-agent's own transcript |

A run exists as soon as its transcript is seen, even before the description arrives; the description fills in
the rest later. A description that cannot be read is skipped and counted, and does not fail the job. The
descriptions are read again at every job (they are tiny and stored by an upsert).

Checked on two real sessions with a sub-agent (`Explore`): each run found its launching call and its prompt.

## File changes

Every successful Edit or Write result becomes a row in `file_change`: the file, the changed **line ranges**
(`hunks`, the transcript's `structuredPatch`; fact 11), lines added and removed, and whether the developer
changed the AI's proposal before accepting it (`user_modified`). No snapshot is needed to know which lines an AI
edit touched. A result counts by its shape (a file path plus a patch list), not by tool name. A new file has no
patch, so the whole content is recorded as one hunk. The code is [`filechanges.py`](../src/ccp/index/filechanges.py).

| Column | Meaning |
| --- | --- |
| `path` | Relative to the session's folder when the file is inside it (the same on every machine, `/` separators), otherwise absolute |
| `absolute_path` | As Claude Code reported it. Contains the user name: restricted |
| `event_id`, `call_event_id` | The result entry, and the AI's tool call that made it |
| `tool` | `Edit`, `Write`, ... (from the call) |
| `prompt_event_id` | The developer's prompt behind the change: the nearest prompt up the tree. For a sub-agent's edit the walk jumps to the call that launched the agent and goes on |
| `change_type` | `added` or `modified` (a deletion cannot be seen in these tools) |
| `actor_class` | Always `ai_edit` for now |

The history of one file: `SELECT ... FROM file_change f JOIN event e USING (event_id) WHERE f.path = '...'
ORDER BY e.raw_object_key, e.raw_line_number`; join `e.is_abandoned` to leave out rewound work.

**Not covered:** files changed by a shell command (a `Bash` or `PowerShell` call, `ai_command` in the schema)
and changes a person makes outside Claude Code. Those need the code snapshots (Step 3).

Results already turned into rows are not read again, so a long session costs no more at each job. Sessions
indexed before this feature are caught up at their next job.

Checked on three real sessions: 34, 126 and 317 edit results gave exactly 34, 126 and 317 rows, each linked
to its call, tool and prompt. Indexing the largest (5,097 events) took 18 s the first time and 0.9 s when repeated.

## Test runs

A shell call (`Bash` or `PowerShell`) whose command **starts** a known test runner gets a row in `test_run`:
pytest, unittest, `npm`/`yarn`/`pnpm test`, jest, vitest, `dotnet test`, `go test`, `cargo test`, maven, gradle,
phpunit, rspec. Only the start of each part of the command counts (`cd app && python -m pytest -q` is a run;
`pip install pytest`, `git commit -m "fix pytest"` and `pytest --collect-only` are not). The code is
[`testruns.py`](../src/ccp/index/testruns.py).

The transcript records no exit code for a success, so the outcome is read from how the call ended:

| How the call ended | `passed` | `exit_code` |
| --- | --- | --- |
| No error | true | 0 |
| An error whose text starts `Exit code <n>` | false | n |
| Interrupted, or an error without an exit code | NULL (unknown) | |
| Refused by a permission decision | no row: it never ran | |

**Limits:** the outcome is the exit code of the *command*, so `pytest | tail` reports the exit code of `tail`;
the number of tests passed and failed is not read; a command with several runners counts once, for the first; a
runner that is not on the list is missed. A call is turned into a row once, when its result has arrived.

Checked on three real sessions: 14, 77 and 39 runs found, one of them a failure, none with an unknown outcome.

## Model usage and cost

- `model_usage`: one row per model **response** (API message), with model, input, output, thinking, cache-read
  and cache-creation tokens. Claude Code writes one entry per content block and repeats the same usage on each,
  so rows are de-duplicated by the message id (the entry's own id if it has none); summing entries would count
  a response about three times over on real data. Join to `event` through `event_id` for the time and actor.
- `session_cost` and `session_model_cost`: the session total and the per-model figures (tokens, cost), from the
  **latest** `cost-state` entry of the main transcript. Cost is read from the transcript, never computed, so no
  price list is kept. This is also the only place that shows helper models (for example the small model that
  writes session titles) and `has_unknown_model_cost`, which says the total is partial.

The two do not have to add up: `cost-state` also counts work that has no entry in the main transcript.
Checked on real sessions: one matched exactly (output tokens 8202 = 8202). With sub-agent streams included, two
sessions got closer but stayed lower (3295 against 5238, 12091 against 15823), and two had no sub-agents and
were off in opposite directions (653591 against 661529; 168540 against 147572). The reason is not yet known
(rewound or resumed work are the first suspects), so **do not treat `model_usage` as the bill**: use
`session_cost` for cost, and `model_usage` for attribution to events. Task 2.8 looks into the gap. A few
rows have the model `<synthetic>` (messages Claude Code makes itself, with zero tokens).

## Thinking

Every reasoning block of an assistant message gets a row in `thinking`, with the block's position and its text.
Claude Code 2.1.289 writes **only a signature, not the text**
([fact 10](verify/transcript-facts.md)), so for now `text` is empty (NULL) and the row only records that the
model thought at that point. A `redacted_thinking` block is marked `is_redacted`. If a later Claude Code
version writes the text, it is kept without any change here.

Thinking text is summarised reasoning, never proof of intent, and can contain anything the model saw. Treat the
table as restricted when access roles are set up (Step 5).

## Limits

- A job whose worker crashed stays `running`; it is not picked up again automatically. Queue a new job by
  uploading again, or reset it by hand. (Recovery is planned with the Step 5 hardening.)
- One event is inserted per statement: about 18 s for a session of 5,000 lines. To be batched if that becomes a
  problem.
- `event.tool_use_ids` (migrations 009 and 010, indexed) holds the ids of the tool calls an entry makes or
  answers. It is what makes "which event holds this tool call, or its result" fast; it is filled when an event
  is indexed.
- The rewind rule was checked on this machine's own transcripts (one rewind, found), not yet on a variety of
  real sessions; the pilot data is the real test (task 2.8). Whether `last-prompt`'s `leafUuid` agrees with it
  is not yet checked.
- The saved SQL questions and the final rebuild check follow in tasks 2.7 and 2.8 of [TODO.md](TODO.md).
