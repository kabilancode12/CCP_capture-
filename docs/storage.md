# Object storage

Raw transcripts and attachments are kept in **MinIO** (S3-compatible). The code is in
[`src/ccp/storage/`](../src/ccp/storage/). Code outside this package talks to `ObjectStore`, never
to the MinIO library, so the storage can be swapped or faked in tests.

```python
from ccp.settings import load_storage_settings
from ccp.storage import connect_store

store = connect_store(load_storage_settings())  # creates the bucket if needed
store.put("some/key", b"bytes")
store.get("some/key")  # raises ObjectNotFoundError if missing
store.put_if_absent("some/key", b"bytes")  # True if written, False if it already existed
```

Errors from the library are translated into `StorageError` (and `ObjectNotFoundError`).
Connections time out after 3 seconds and retry server errors three times.

## Key layout

Keys are built only in [`keys.py`](../src/ccp/storage/keys.py). Every outside value (session id,
agent id) is validated, so a value such as `../other` cannot escape its folder.

| Key | Holds |
| --- | --- |
| `sessions/<session_id>/main/<offset>.jsonl` | A chunk of the session transcript |
| `sessions/<session_id>/agent-<agent_id>/<offset>.jsonl` | A chunk of a sub-agent transcript |
| `sessions/<session_id>/tool-results/<name>` | A large tool output Claude Code kept in a file next to the transcript (redacted text). `tool-results` is never a stream name, and `<name>` never ends in `.jsonl` |
| `sessions/<session_id>/tool-results/<name>@<hash>` | A later version of that file, if it changed (`<hash>`: the first 16 hex digits of the new content's SHA-256). Stored objects are never replaced |
| `attachments/<first 2 hex>/<sha256>` | An attachment body, stored once |

`<offset>` is the byte position in the source file where the chunk starts, zero-padded to 12
digits. Uploading the same chunk again writes to the **same key**, so a retry cannot create a
duplicate, and listing keys alphabetically gives the chunks in order.

## Attachment de-duplication

About 80 % of a Claude Code transcript is `attachment` entries (skill, tool and agent listings,
prompt snapshots), and the same content repeats in every session. For each attachment line:

1. Large values (1 KB or more) are stored under the SHA-256 hash of their content and replaced by
   a pointer: `{"$ccp_ref": "<sha256>"}`. Two levels are looked at:
   - the line's own values, such as `rendered`;
   - the values **inside** the `attachment` object, such as a skill listing's `content` or a
     prompt snapshot's `systemPrompt`. Its small values, above all its `type`, stay in the line,
     so SQL can still tell what kind of attachment it is.
2. If the same content is already stored (from this or any other session), nothing is written.
3. `restore_line` rebuilds the original line **byte-for-byte**.

A stored line looks like this (shortened):

```json
{"type":"attachment","uuid":"…","attachment":{"type":"skill_listing","content":{"$ccp_ref":"<sha256>"},"skillCount":42,"isInitial":true}}
```

An `attachment` object made only of small values stays as it is, even if it is 1 KB or more in
total; an `attachment` value that is not an object (a long string, a list) is moved out as a
whole.

**Older lines.** Lines stored before this form replaced the whole `attachment` object by one
pointer (`"attachment":{"$ccp_ref":"<sha256>"}`), so their attachment `type` is not in the line.
`restore_line` still rebuilds them exactly; they are not rewritten.

```python
from ccp.storage.attachments import externalise_line, restore_line

result = externalise_line(
    redacted_line, store
)  # result.text, result.new_objects, result.reused_objects
original = restore_line(result.text, store)
```

On a developer's machine the bodies are not written to MinIO directly: the capture client sends
each one to the ingestion API (`PUT /v1/attachments/{digest}`, see
[ingestion-api.md](ingestion-api.md)), so the machine needs no storage credentials.

Guarantees and rules:

- **Redact first, then externalise.** The stored objects and their hashes are of clean content.
- **Exactness is checked, not assumed.** Claude Code writes compact JSON, so re-serialising
  reproduces a line exactly. Before anything is stored, the rebuilt line is compared with the
  original; a line that would not match (unusual number formats, different spacing) is left
  untouched.
- **Identifying fields** (`type`, `uuid`, `parentUuid`, `sessionId`, `session_id`, `timestamp`)
  are never moved out, neither from the line nor from inside its `attachment` object.
- Only `attachment` entries are touched; user and assistant lines are never changed.

Measured on a real test session (Claude Code 2.1.289, 184 lines, redacted first): lines shrank to
22 % of their size, 15 unique objects were stored, 13 repeats were reused, and the total kept was
65 % of the original. 0 lines failed to rebuild exactly. Across many sessions the saving grows,
because the same listings repeat (not yet measured). These figures were taken with the earlier
form (the whole `attachment` object moved out); the current form keeps the small values in the
line, so lines are slightly larger. Checked with the current form on a longer real session
(868 lines, 56 attachment lines): every attachment line kept its `type`, and 0 lines failed to
rebuild exactly.

## Tests

`tests/unit/storage/` uses an in-memory store. `tests/integration/test_object_store.py` runs
against the real MinIO on a throwaway bucket (`ccp-test-<random>`), removed afterwards; it is
skipped if MinIO is not running.
