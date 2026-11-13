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

Because of (2) and (3), a crash leaves a chunk fully indexed or not at all, a repeated job adds nothing, and
a chunk that arrives later is simply added. A failed job is tried again up to three times, then stays
`failed` (see `job.status`); a new chunk of the session queues a fresh job.

Sub-agent entries are stored under the **parent's** session id, with the actor `subagent`.

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
- One event is inserted per statement: fine for sessions of thousands of lines, to be batched if indexing
  becomes slow.
- Only events are filled so far. The conversation tree (abandoned branches), model usage, sub-agent runs, file
  changes and test runs follow in tasks 2.2 to 2.6 of [TODO.md](TODO.md).
