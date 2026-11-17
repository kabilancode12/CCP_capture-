# Ingestion API

The server side of capture: developers' machines upload (already redacted) transcript chunks
here. The code is in [`src/ccp/ingest/`](../src/ccp/ingest/).

## Running it

```powershell
ccp-db migrate        # the tables must exist first
ccp-ingest            # serve the API; also closes idle sessions in the background
                      # (or: python -m ccp.ingest)
```

It reads its settings from `.env` / the environment ([configuration.md](configuration.md)):
`CCP_API_HOST`, `CCP_API_PORT`, `CCP_INGEST_TOKEN`, `CCP_MAX_CHUNK_BYTES`, plus the database and
MinIO settings. On start it connects to MinIO and creates the bucket if it is missing.

The server **checks the database schema when it starts** and refuses to start if migrations are
pending: `The database schema is behind this server (pending: 003_...). Run: ccp-db migrate`. Use
`--migrations-dir` if the migration files are not in `db/migrations`, or `--skip-schema-check` to
start anyway. If the migrations folder or the database is not reachable, the check is skipped with a
warning and `/health` reports what is down.

## Endpoints

### `GET /health`

No token needed. Checks that the database and the object store both answer.

| Status | Body |
| --- | --- |
| 200 | `{"status": "ok", "database": "ok", "storage": "ok"}` |
| 503 | `{"status": "degraded", "database": "unavailable", "storage": "ok"}` (which part is down) |

### `PUT /v1/sessions/{session_id}/streams/{stream}/chunks/{offset}`

Uploads one chunk. Send `Authorization: Bearer <token>` and the chunk as the request body.

- `stream` is `main` for the session transcript, or `agent-<agent_id>` for a sub-agent.
- `offset` is the byte position where the chunk starts in the source file.
- The body must be UTF-8 text made of **complete lines, each a JSON object**, ending with a newline.

The same URL always means the same chunk, so **uploads are idempotent**:

| Status | Meaning |
| --- | --- |
| 201 | Stored for the first time |
| 200 | Already stored with identical content (a retry): nothing is written again |
| 409 | A *different* chunk is already stored at this offset |
| 400 | Unsafe `session_id` or `stream` (letters, digits, `.`, `_`, `-` only) |
| 401 | Missing or wrong token |
| 413 | Chunk larger than `CCP_MAX_CHUNK_BYTES` |
| 422 | Not UTF-8, not complete JSON-object lines, or empty (the message names the line, never its content) |
| 503 | Storage or database temporarily unavailable: retry later |

Successful responses are `{"stored": true|false}`. The API does not return a "next offset": the
uploaded text is redacted, so its size is not the size of the source bytes. The client keeps its own
position in the source file ([capture-client.md](capture-client.md)).

Example:

```powershell
curl.exe -X PUT -H "Authorization: Bearer $env:CCP_INGEST_TOKEN" --data-binary "@chunk.jsonl" `
  http://127.0.0.1:8000/v1/sessions/4a64fad3-ca80-49bc-a42e-87f42b332b4b/streams/main/chunks/0
```

### `PUT /v1/attachments/{digest}`

Uploads one attachment body: the large, repeated value that the client moved out of a transcript
line (see [storage.md](storage.md#attachment-de-duplication)). Send `Authorization: Bearer
<token>` and the body (compact JSON, UTF-8). `digest` is the SHA-256 of the body, as 64 lowercase
hex digits.

Developers' machines send attachments **here**, not to MinIO, so they never hold storage
credentials. The server checks that the body really hashes to `digest`, so a client cannot store
content under a wrong name or overwrite another attachment: the same key always holds the same bytes.

| Status | Meaning |
| --- | --- |
| 201 | Stored for the first time |
| 200 | Already stored (the same content, uploaded before by this or another session): nothing written |
| 400 | `digest` is not 64 lowercase hex digits |
| 401 | Missing or wrong token |
| 413 | Larger than `CCP_MAX_CHUNK_BYTES` |
| 422 | Empty, not UTF-8, not JSON, or the body does not hash to `digest` |
| 503 | Storage temporarily unavailable: retry later |

Successful responses are `{"stored": true|false}`.

### `PUT /v1/sessions/{session_id}/environment`

Stores where a session ran: Claude Code version, surface, shells, operating system, user and host
name, and the account ([environment.md](environment.md)). Send the bearer token and a JSON object
with any of the known fields. Fields that are present replace the stored value; missing fields
leave it unchanged, so the record can be sent in parts and repeated safely.

| Status | Meaning |
| --- | --- |
| 200 | Stored or merged: `{"recorded": <number of fields in the request>}` |
| 400 | Unsafe `session_id` |
| 401 | Missing or wrong token |
| 413 | Larger than `CCP_MAX_CHUNK_BYTES` |
| 422 | Not JSON, empty, an unknown field, or a value that is not a short string without control characters |
| 503 | Database temporarily unavailable: retry later |

This is personal data (user name, host name, account e-mail, Git identity): restrict access to it.

Since migration 016 the record also has `account_uuid`, `full_name`, `organisation_role`, `machine_key` (a
hash, never the raw machine id), `folder`, `git_remote` and `git_user_name` / `git_user_email`. A Git remote is
stored without credentials even if a client sent them. Each record links the chat to its Claude account and
project (`ccp.db.hierarchy`, see [database.md](database.md#the-chat-hierarchy-migration-016)). An older server
refuses these fields with 422; the client then sends the others again.

### `POST /v1/sessions/{session_id}/lifecycle`

Records that a **session** of the chat started (SessionStart hook) or ended (SessionEnd hook), as a row of
`chat_session`. Send the bearer token and one JSON object:

```json
{"kind": "start", "run_key": "6f1c2d3e-...", "at": "2026-11-15T17:35:02+00:00", "source": "resume", "commit": "<sha or null>"}
{"kind": "end",   "run_key": "6f1c2d3e-...", "at": "2026-11-15T18:00:00+00:00", "reason": "logout", "commit": null}
```

The run key makes it idempotent: the same event again changes nothing, and a start and an end with the same key
fill one row, in either order. A start also ends an earlier session of the chat that is still open
(`end_reason = inferred`: Claude Code runs no SessionEnd before `/compact`), reopens the chat for the idle closer,
and sets the chat's first `start_source` and `start_commit`; an end sets the chat's `end_reason` and `ended_at`.
A source or reason this server does not know is stored as `unknown` or `other` rather than refused. Unknown keys
are ignored.

| Status | Meaning |
| --- | --- |
| 200 | Recorded: `{"recorded": true, "seq": <the session's number in the chat>}` |
| 400 | Unsafe `session_id` |
| 401 | Missing or wrong token |
| 409 | The run key belongs to another chat |
| 413 | Larger than 4 KB |
| 422 | Not JSON, or `kind`, `run_key` (8 to 64 letters, digits, dashes), `at` (ISO 8601) or `commit` (hex) malformed |
| 503 | Database temporarily unavailable: retry later |

### `PUT /v1/sessions/{session_id}/tool-results/{name}`

Stores one **tool-result file**: a large tool output that Claude Code keeps in
`<chat folder>/tool-results/<name>` and only refers to from the transcript. The body is the file's text,
**already redacted** by the client, as UTF-8. It is stored in MinIO at `sessions/<id>/tool-results/<name>`
(see [storage.md](storage.md)); a changed file is stored next to it as `<name>@<first 16 hex of its SHA-256>`,
so nothing stored is ever replaced. `key` in the answer is where this content is stored; the client reports the
file's redactions against it.

| Status | Meaning |
| --- | --- |
| 201 | Stored for the first time: `{"stored": true, "key": "sessions/<id>/tool-results/<name>"}` |
| 200 | Already stored unchanged (`{"stored": false, "key": ...}`, nothing written), or a changed version stored next to it (`{"stored": true, "key": "...<name>@<hash>"}`) |
| 400 | Unsafe `session_id` or name (letters, digits, `.`, `_`, `-`; at most 128; not ending in `.jsonl`) |
| 401 | Missing or wrong token |
| 413 | Larger than `CCP_MAX_CHUNK_BYTES` |
| 422 | Not UTF-8 text |
| 503 | Storage temporarily unavailable: retry later |

`tool-results` is not allowed as a stream name for chunks, so a tool result can never look like a transcript
chunk to the indexer.

### `POST /v1/sessions/{session_id}/redactions`

Reports where the client's redaction rules fired. Send the bearer token and
`{"records": [{"rule_id": "email", "location": "<chunk key>:<line>:<JSON path>", "occurrences": 2}]}`.
It never carries the removed value. Repeating a report is safe: a record is identified by session,
rule and location, and a repeat only sets the same count again.

| Status | Meaning |
| --- | --- |
| 200 | Recorded: `{"recorded": <number of records>}` |
| 400 | Unsafe `session_id` |
| 401 | Missing or wrong token |
| 413 | Larger than `CCP_MAX_CHUNK_BYTES` |
| 422 | Not JSON, or a record is malformed (rule id not a short identifier, location empty or over 512 characters, bad count, more than 5000 records) |
| 503 | Database temporarily unavailable: retry later |

## What a chunk upload does

1. Check the token (compared in constant time) and the ids; check size and shape.
2. Store the chunk under `sessions/<session_id>/<stream>/<offset>.jsonl` (see [storage.md](storage.md)).
3. In the database, in one transaction: create the session row if it is new, or note the
   activity and reopen it if it had been marked closed, and **queue an index job** unless one is
   already waiting for that session (so many chunks queue one job).

Step 3 runs on every upload, including retries. If the server stops between steps 2 and 3, the
client's retry finds the chunk already stored and completes the database step: the system heals
itself without the client doing anything special.

## Idle sessions

A session is **open** while chunks keep arriving. If nothing arrives for
`CCP_SESSION_IDLE_MINUTES` (default 30), it is marked **closed**: the SessionEnd hook may never have
run, the laptop may have been closed, the network may have dropped for good. Closing a session:

- sets `status = 'closed'`, `ended_at` to the time of its last activity, and `end_reason` to
  `idle_timeout` (an end reason that is already known, such as `prompt_input_exit`, is kept);
- ends a session of the chat (`chat_session`) that never received its end, at the last activity, with
  `end_reason = idle_timeout`;
- queues one final index job, so nothing indexed earlier is stale (an already-queued job is not
  duplicated);
- is **undone by a late chunk**: the next upload reopens the session.

The server does this itself in the background: once at start-up and then every minute (sooner
for very short timeouts). To run it by hand or from a scheduler:

```powershell
ccp-ingest close-idle      # closes idle sessions once, prints how many, then exits
```

It is safe to run several times, or alongside the server: one atomic SQL statement does the work,
and a session that receives a chunk at the same moment is **not** closed. The row is re-checked
after the upload commits (tested with a real lock, see `test_idle_sessions.py`). A failure in the
background loop (for example the database being down) is logged and retried; it does not stop the
loop.

## Design choices

- **Starlette, not FastAPI.** FastAPI depends on `pydantic-core`, a compiled library that company
  Application Control policy blocks on some machines. Starlette (which FastAPI is built on) and
  uvicorn are pure Python. Validation is explicit and small, in `chunks.py` and `keys.py`.
- **One uploader per session** is assumed (a developer's machine). Two machines sending different
  content for the same offset at the same instant is not supported.
- **The server does not look at content.** Redaction happens on the developer's machine before
  upload; the server only checks the shape. Errors never echo content, tokens or internal detail.
- **Early refusals read the request body first.** Answering 401 or 400 and closing the connection
  while the body is unread makes the operating system reset the connection (seen on Windows), and
  the client then sees a network error instead of the real status, so a permanent refusal looks like a
  temporary outage. The body (within the size limit) is read and dropped before the refusal is sent.
- **A new database connection per request** keeps the code simple. A connection pool can be added
  if load requires it.

## Tests

- `tests/unit/ingest/`: the API with in-memory fakes (no services needed): authentication,
  validation, idempotency, conflicts, size limits, and failure of each dependency.
- `tests/unit/ingest/test_closer.py`: the timer loop (keeps running after errors, stops with the
  server). `tests/integration/test_idle_sessions.py`: the closing SQL against real PostgreSQL.
- `tests/unit/ingest/test_attachment_upload.py`: the attachment endpoint (digest check, idempotency,
  limits, outages).
- `tests/unit/ingest/test_lifecycle_api.py`, `test_tool_result_api.py`: the lifecycle and tool-result
  endpoints. `tests/integration/test_chat_hierarchy.py`: both, and the account and project linking, against
  the real PostgreSQL and MinIO.
- `tests/integration/test_ingest_api.py`: the API against the real PostgreSQL and MinIO
  (throwaway database and bucket), including recovery after a database outage, and one test over
  a real network socket with uvicorn.
