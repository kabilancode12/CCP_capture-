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
| Delete the index of sessions and queue a rebuild | `ccp-index rebuild --session ID` or `--all` |
| Rebuild now and prove the result is identical | `ccp-index rebuild --all --check` |

Settings are the `POSTGRES_*` and `MINIO_*` variables ([configuration.md](configuration.md)). If the launcher
is blocked on your machine, use `python -m ccp.index run`. `--poll-seconds N` sets how long it waits when the
queue is empty (default 5).

If the database fails while `run` takes a job or records its outcome (a restart, a network drop), the error is
logged and the worker waits `--poll-seconds` and goes on: it outlives a short outage. `run --once` instead stops
with the error and exit code 1, so a script notices.

## Rebuilding, and proving it works

The index is only worth having if it can be rebuilt from the stored transcripts. `ccp-index rebuild` deletes the
index of the chosen sessions (`--session ID`, repeatable, or `--all`). Without `--check` it queues a job per
session, for a running `ccp-index run` to build again. With **`--check`** it builds again right away and
compares the result with what was there before, table by table (exit code 1 if anything differs):

```text
$ ccp-index rebuild --all --check
cab7846a-...: identical (212 events)
...
12 of 12 session(s) identical
```

The comparison is a **fingerprint** per table: a hash of the session's rows written without the database's own
ids (an event is identified by the object and line it came from), so two builds of the same transcript have
the same fingerprint. A difference names the tables, for example `DIFFERENT in file_change, test_run`; after the
check the index is the rebuilt one, so a difference is also repaired. The code is
[`rebuild.py`](../src/ccp/index/rebuild.py).

Rebuilt: events, thinking, model usage and cost, compactions, sub-agent runs, file changes, test runs, and the
session's start time and branch. **Never touched:** the session row itself, the environment record and the
redaction log, which come from the capture client and not from the transcript. A rebuild leaves one `done` job
behind for each session it checked (the job is queued first, so that a crash half way is finished by a worker).

Checked on five real sessions from this machine, with their sub-agents (7,228 events in all): every rebuild
was identical. Running it on the sessions captured during the pilot is the last step of [pilot.md](pilot.md).

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

A line that is not valid UTF-8, or not a JSON object, is skipped and counted (the job log says how many); the
other lines keep their own line numbers. One bad line never blocks the rest of the session.

The events of a chunk are sent **in batches** of 1,000, one statement per batch (`INSERT ... SELECT FROM
unnest(...)`), and the thinking blocks likewise. The rows are exactly those that one statement per event gave:
`tests/integration/test_indexer_batching.py` stores the same transcripts both ways and compares the rebuild
fingerprints. Storing a synthetic 5,000-line chunk took 11 to 14 s one event at a time and about 1.0 s in
batches; indexing the whole session (with the refresh) went from 13 to 14 s to 3 to 3.5 s.

### Jobs and workers

| Situation | What happens |
| --- | --- |
| A job is taken | `status = 'running'`, `attempts + 1`, `started_at = now()` (migration 014) |
| The job succeeds | `done` |
| The job fails | Queued again if it has attempts left (3 in all) and the session has no other queued job; otherwise `failed` |
| The job fails just as the API queues a new job for the session | `done`: the new job indexes the session (at most one queued job per session, migration 002) |
| A worker died: its job has been `running` for over 30 minutes | Before taking a job, a worker puts it back: queued again if it has attempts left, `done` if the session already has a queued job, `failed` if it has no attempts left. A job taken before migration 014 counts from its creation time |
| Two workers work on the same session at once (a new chunk queued a job while the old one ran) | They take turns: storing a chunk, refreshing the derived rows and the rebuild's reset each hold a lock on the session (`pg_advisory_xact_lock(hashtext(session_id))`) for their transaction. Other sessions do not wait |

The 30 minutes are `STALE_JOB_MINUTES` in [`repository.py`](../src/ccp/index/repository.py). A job that really
runs longer than that is harmless: it would be started again, wait for the lock, and add nothing.

Sub-agent entries are stored under the **parent's** session id, with the actor `subagent`. The stream's
name is in the event's `raw_object_key` (`sessions/<id>/agent-<agent id>/...`).

## What an entry becomes

| Transcript entry | `event_type` | `actor` |
| --- | --- | --- |
| user, a prompt the developer typed | `prompt` | `human` (`subagent` inside a sub-agent) |
| user, a slash command the developer typed | `slash_command` | `human` |
| user, the output of a local command | `command_output` | `system` |
| user, a message from another agent or a background task | `agent_message` | `system` (`subagent` for a sub-agent's hand-back) |
| user, injected by Claude Code (`isMeta`) | `meta` | `system` |
| user, tool results | `tool_result` | `system` |
| user, compaction summary | `compaction_summary` | `system` |
| assistant, with a tool call | `tool_call` | `ai` (`subagent` inside a sub-agent) |
| assistant, with text | `response` | `ai` (`subagent` inside a sub-agent) |
| assistant, only thinking | `thinking` | `ai` (`subagent` inside a sub-agent) |
| system | its `subtype`, for example `compact_boundary` | `system` |
| any other type | the type itself, for example `cost-state`, or `unknown` | `system` |

### Who wrote a "user" entry

Many `user` entries were not typed by a person. The parser tells them apart with these tests, in this order
(the code is `_classify_user` in [`parser.py`](../src/ccp/index/parser.py)); checked on a real transcript of a
later Claude Code version:

1. `isCompactSummary` is true: `compaction_summary`.
2. The message holds tool results: `tool_result`.
3. The text starts with `<local-command-stdout>`, `<local-command-stderr>`, `<local-command-caveat>`,
   `<bash-stdout>` or `<bash-stderr>`: `command_output` (what `/model`, `/context` or `!` shell mode printed,
   and the caveat Claude Code writes before it).
4. `origin.kind` is there and is not `human` (for example `task-notification`: a background task finished;
   `peer`: another agent's message): `agent_message`. A `peer` message with `origin.handback = true` is a
   sub-agent handing back its report, so its actor is `subagent`; otherwise `system`.
5. `isMeta` is true (a skill's body, a reminder): `meta`.
6. The text starts with `<command-name>` or `<command-message>`: `slash_command`. (A skill's body starts the
   same way but is marked `isMeta`, so step 5 took it.)
7. Anything else is a `prompt`.

An entry without `origin` (Claude Code 2.1.289 wrote none, nor does a later version on slash commands) is
told apart by the other steps only, so a plain prompt of an old transcript is still a `prompt`.

**The developer's prompts** are `prompt` and `slash_command` events with the actor `human`. They are what the
rewind rule counts and what `file_change.prompt_event_id` points at: an edit made after a task notification or
a sub-agent's report is traced back past it to the prompt the developer typed. The views of migrations 011 and
013 (`v_human_decision`, `v_compaction.prompts_before`, `d_session.prompts`) count `prompt` events only, so
they now leave out slash commands, injected messages and agents' messages.

Sessions indexed before this change keep the old types until they are rebuilt (`ccp-index rebuild --all`); for
a session with such entries, `rebuild --check` reports `DIFFERENT in event` once (and possibly `file_change`),
which is the new classification, not damage.

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
have happened. Only the developer's prompts count (typed prompts and slash commands, see above): tool calls
(which also give an entry two children), messages Claude Code injects itself (`isMeta`), command output,
messages from agents and background tasks, and sub-agent prompts do not. A compaction boundary
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
| `agent_type`, `description`, `model`, `spawn_depth`, `tool_use_id` | the description. The model is its `model`, else its `pickedEffortModel` (what real files hold), else the `resolvedModel` of the parent's result for the launching call |
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
to its call, tool and prompt. Indexing the largest (5,097 events) took 18 s the first time (one event per statement, before batching) and 0.9 s when repeated.

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

## Asking questions

The six questions the audit trail must answer are views on top of these tables: see
[queries.md](queries.md).

## Limits

- A job whose worker crashed is queued again only after 30 minutes (see [Jobs and workers](#jobs-and-workers)).
- `event.tool_use_ids` (migrations 009 and 010, indexed) holds the ids of the tool calls an entry makes or
  answers. It is what makes "which event holds this tool call, or its result" fast; it is filled when an event
  is indexed.
- The rewind rule was checked on this machine's own transcripts (one rewind, found), not yet on a variety of
  real sessions; the pilot data is the real test (task 2.8). Whether `last-prompt`'s `leafUuid` agrees with it
  is not yet checked.
- The final rebuild check on real pilot data is task 2.8 of [TODO.md](TODO.md).
