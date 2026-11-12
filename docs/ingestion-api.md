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

Successful responses are `{"stored": true|false, "next_offset": <offset + chunk size>}`.

Example:

```powershell
curl.exe -X PUT -H "Authorization: Bearer $env:CCP_INGEST_TOKEN" --data-binary "@chunk.jsonl" `
  http://127.0.0.1:8000/v1/sessions/4a64fad3-ca80-49bc-a42e-87f42b332b4b/streams/main/chunks/0
```

## What an upload does

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
- **A new database connection per request** keeps the code simple. A connection pool can be added
  if load requires it.

## Tests

- `tests/unit/ingest/`: the API with in-memory fakes (no services needed): authentication,
  validation, idempotency, conflicts, size limits, and failure of each dependency.
- `tests/unit/ingest/test_closer.py`: the timer loop (keeps running after errors, stops with the
  server). `tests/integration/test_idle_sessions.py`: the closing SQL against real PostgreSQL.
- `tests/integration/test_ingest_api.py`: the API against the real PostgreSQL and MinIO
  (throwaway database and bucket), including recovery after a database outage, and one test over
  a real network socket with uvicorn.
