# Capture client

The part of the platform that runs on a **developer's machine**: it reads Claude Code's transcript
files and uploads them to the [ingestion API](ingestion-api.md). The code is in
[`src/ccp/capture/`](../src/ccp/capture/). The Claude Code hooks that call it are added in task 1.9.

It needs no database or storage credentials, and **no packages at all beyond this project**: it talks
only to the API, using the standard library for HTTP. A test checks that importing it loads none of the
server's packages (MinIO, the PostgreSQL driver, the web framework), so each hook starts in about
0.15 s.

## Commands

```powershell
ccp-capture sync --session-id <id> <path to the session transcript .jsonl>
ccp-capture catch-up        # upload what earlier sessions left behind
ccp-capture environment --session-id <id> [transcript]   # record where the session runs
```

`ccp-capture snapshot` takes a code snapshot now; `ccp-capture flush` is what the SessionEnd hook starts in the
background (send the waiting session end, upload the rest if there is a transcript, then snapshot). See
[snapshots.md](snapshots.md).

Claude Code calls `ccp-capture hook` for each event: see [hooks.md](hooks.md). `ccp-capture hook-settings`
prints the settings that register the hooks.

If the `ccp-capture` launcher is blocked on your machine, use `python -m ccp.capture` (or call
`ccp.capture.cli.main`). Both print one line per session, for example
`4a64fad3-...: 2 chunk(s), 3 attachment(s) uploaded`, and exit with 0 on success or 1 if anything
failed. A failure is never fatal for Claude Code: the hook wrapper (task 1.9) always exits 0.

The `environment` command collects the session's environment record (version, surface, shell,
OS, account) and sends it; see [environment.md](environment.md).

## Settings

Environment variables only (see [configuration.md](configuration.md#capture-client)). **A `.env`
file is deliberately not read:** the client runs inside whatever project the developer is working
on, and that project could contain a `.env` that points the client, and its token, at another server.

## What a sync does

For each stream (the session transcript, plus every sub-agent file under
`<session>/subagents/`, including those of sub-agents started by the Workflow tool under
`<session>/subagents/workflows/<run id>/`; a workflow's `journal.jsonl` is not uploaded):

1. **Lock** the stream, so two hooks never upload it at the same time.
2. **Read** the complete lines after the saved position. A half-written last line waits.
3. **Group** them into chunks of about 1 MiB (never splitting a line).
4. For each chunk, in order:
   1. **Redact** every line ([redaction.md](redaction.md)); blank lines are skipped.
   2. **Move large attachment values out** and replace them by pointers
      ([storage.md](storage.md#attachment-de-duplication)).
   3. **Upload** the attachment bodies, then the chunk, then the report of where redaction fired.
   4. **Only then** save the new position.

Sub-agent transcripts are uploaded as the stream `agent-<id>` wherever they are found (agent ids
are unique), and their `.meta.json` files (small, written once) as the stream `agent-<id>.meta`.
The description of a sub-agent started by the Workflow tool does not name its run (observed on Claude
Code 2.1.325); the folder does, so the client adds it as `"ccpWorkflowRunId": "<run id>"` to that
uploaded line.

After the transcripts, a sync also sends:

- **Session starts and ends** waiting in the state folder (see [hooks.md](hooks.md#sessions-of-a-chat-start-and-end)).
- **Tool-result files.** Claude Code keeps large tool outputs in `<session>/tool-results/<name>` (for
  example `bs12jynbi.txt`) and only refers to them from the transcript. Each file is read as UTF-8 text,
  **redacted as one piece of text** with the same rules as the transcript (a file that holds a JSON object
  or list is first redacted value by value, as transcript lines are, because a secret inside a JSON string
  is escaped; it is written out again as JSON only if that found something), and uploaded to
  `PUT /v1/sessions/{id}/tool-results/{name}`; where rules fired is reported with the location
  `<key>:0:`, where `<key>` is the object key the server answered (`sessions/<id>/tool-results/<name>`, or
  `...<name>@<hash>` for a changed version). A file is remembered by its size and modification time, so it is
  uploaded once, and again only if it changes. A file larger than 8 MiB, a name that is not safe, a name
  ending in `.jsonl` (it would look like a transcript chunk) or a file the server refuses is skipped with a
  log line, once. A refusal never counts as a refused chunk. If the redaction report fails after the file was
  stored, the file is sent again at the next sync; the server stores nothing new and records each redaction
  site once. The prompt hook (`UserPromptSubmit`) leaves tool-result files for a later sync.

Both use endpoints an older server does not have. Its 404 or 405 is logged once; the client asks again an
hour later (per session) and the transcript upload carries on.

The environment record has fields an older server does not know; if such a server refuses the record
(422), the client sends again with only the fields it knows, remembers the hash of what it sent (not of the
whole record), and the hook sends the whole record again an hour later.

When the API **refuses** an upload:

- **401 or 403**: the token is not accepted. Every other upload would be refused too, so the run
  stops at once (no other stream or session is tried) and the log says to check
  `CCP_INGEST_TOKEN`. The position does not move: nothing is lost once the token is fixed.
- **Any other refusal** (such as 413 or 422): the same chunk is tried again at the next sync. After
  3 refusals in a row, a `ccp-withheld` placeholder (`"reason":"chunk refused by the API"`, with the
  status and the number of lines and bytes, never content) is uploaded in its place and the
  position moves past it, so one chunk cannot block the stream forever. If the placeholder is
  refused too, the position still moves; the log says so.

An answer that is not the API's own (a proxy or captive portal page instead of JSON, or no
`stored` flag) is treated like "API unavailable": nothing moves and the next sync tries again.

## The promises, and how they are kept

| Promise | How |
| --- | --- |
| **Nothing is lost** if the API is down or the machine is offline | The saved position moves only after a chunk is fully uploaded. The transcript file is the queue: the next sync repeats the same steps from the same place. No second copy of the data is kept on disk |
| **Nothing is duplicated** on a retry | A chunk is named by session, stream and source offset, and redaction is deterministic, so a retry sends identical bytes to the same key and the server answers "already stored" |
| **Losing the state folder is harmless** | Starting from zero re-sends the same chunks at the same offsets; the server answers "already stored" for each |
| **Nothing sensitive leaves the machine** | Redaction runs before anything is uploaded; attachment bodies are redacted before they are stored |
| **A bad line cannot block a stream forever, or leak** | A line that cannot be processed becomes a `ccp-withheld` placeholder; a line that is not a JSON object becomes `ccp-invalid-line`. Both are reported as redaction records. A line longer than the read window (16 MiB) becomes a `ccp-withheld` placeholder giving only its size, and a chunk refused 3 times in a row is replaced by one (see above) |
| **A hook is not held up** | A sync stops cleanly after `CCP_CLIENT_BUDGET_SECONDS` (default 20) and the next sync continues |
| **The token is not sent elsewhere** | Redirects are refused (urllib would otherwise forward the `Authorization` header); no `.env` is read; errors never contain the token or content |

## What is stored on the machine

Only small files in `CCP_STATE_DIR` (default `~/.ccp/capture`), never transcript content:

```text
<state_dir>/<session_id>/<stream>.offset     a number: source bytes uploaded so far
<state_dir>/<session_id>/<stream>.lock       exists while that stream is being uploaded
<state_dir>/<session_id>/transcript_path     where the transcript is, so catch-up can find it
<state_dir>/<session_id>/<name>.marker       a small value: environment-sent: the hash of the last
                                             environment record sent; environment-done: "1" once
                                             complete, "partial <time>" if an older API took part,
                                             refused-<stream>: "<offset> <refusals in a row>",
                                             run-key: the current session's run key,
                                             lifecycle-<kind>-<key>: a start or end waiting to be sent,
                                             tool-result-<hash of name>: "<size>:<mtime>" once uploaded,
                                             unsupported-<feature>: when an older API said 404
```

A lock older than two minutes is assumed to belong to a crashed process and is taken over.

## Redaction reports

For each uploaded chunk the client reports where rules fired: the rule id and a location
`<chunk key>:<line in the chunk>:<JSON path>`, with a count. The report never contains the removed
value, and the location uses the redacted form of any key. The server stores these in
`redaction_log` (migration 003 makes repeating a report safe).

## Limits

- **A single line larger than the server's limit** (default 8 MiB) cannot be uploaded; after 3
  refusals its chunk is replaced by a placeholder and the stream moves on. Large attachment lines
  are normally shrunk well below this.
- **A single line larger than the read window** (16 MiB) is never read into memory: it is skipped
  and a placeholder with its size stands in for it.
- **A transcript that is replaced or truncated** (shorter than the saved position) is logged and
  nothing new is read from it until it grows past the saved position.
- **Sub-agent `.meta.json` files are assumed not to change** after they are created `[verify]`.
- **If a transcript file is deleted before it is uploaded**, that data is gone. Claude Code removes
  old transcripts after its retention period (30 days by default). Catch-up at each session start
  keeps the window small.
- **Catch-up looks at every session the client has ever seen** (one small read each). Pruning old
  entries is not implemented yet.

## Tests

- `tests/unit/capture/`: reader, state and locking, pipeline, HTTP client (against a local test
  server that can misbehave), sync logic with a fake API, and the command.
- `tests/unit/capture/test_client_additions.py`: lifecycle events, tool-result files, the older-server
  fallbacks and the Workflow run id.
- `tests/integration/test_chat_hierarchy.py`: tool-result files and lifecycle events through the real API.
- `tests/integration/test_capture_client.py`: the real client against the real API, PostgreSQL and
  MinIO: stored data is redacted, attachments are stored once, **killing the API mid-session loses
  nothing and duplicates nothing**, and losing the state folder creates no duplicates.
