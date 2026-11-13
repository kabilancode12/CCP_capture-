# Todo plan

Working list, done one task at a time. Mark a task `[x]` when its "done when" is true.
The design is in [architecture.md](architecture.md); the long-term phases are in
[reference/project-plan.docx](reference/project-plan.docx).

## Step 0: finish verify

- [x] **0.1** Install pre-commit and secret scanning; run all hooks on the repo.
  *Done when:* hooks pass; a planted secret is blocked. (gitleaks runs through Docker.)
- [x] **0.2** Hook probe: logs which hooks fire and the keys of each payload, never content.
  *Done when:* it has tests and can be registered on all hooks including SessionEnd.
  (Live check: headless run on Windows fired SessionStart, UserPromptSubmit, Stop, SessionEnd.)
- [x] **0.3** Run the [scripted real session](verify/test-session-script.md) on Windows (you,
  guided by Claude). *Done when:* a transcript and a hook log exist.
- [x] **0.4** Run `ccp-verify` on that transcript and fix inspector gaps.
  *Done when:* the report covers thinking, edit results, tree, sub-agents and compaction.
- [x] **0.5** Fill in [transcript-facts.md](verify/transcript-facts.md).
  *Done when:* no `?` remains for anything tested, each with its design impact.
- [x] **0.6** Run `ccp-verify env` on the other surfaces available (PowerShell, Git Bash, VS Code).
  *Done when:* the environment matrix is updated.
- [x] **0.7** Settle the Step 1 decisions (below). *Done when:* written down here.

**Gate:** facts note and decisions signed off. (Only 0.6 is still open, and it does not block Step 1.)

### Decisions for Step 1 (made)

| Decision | Chosen | Reason |
| --- | --- | --- |
| Database migrations | Plain numbered SQL files in `db/migrations/`, applied in order by a small tested runner | Readable in review; no extra dependency; the draft schema is already SQL |
| Capture client language | Python; Git snapshots called through `subprocess` | Transcript parsing, redaction, queueing and sub-agent handling need real tests ([facts](verify/transcript-facts.md) 4-7, 17) |
| Attachment entries (about 83% of bytes) | Store everything, de-duplicated by content hash, after redaction | Layer 1 stays complete; storage and upload shrink ([facts](verify/transcript-facts.md) 16) |
| Retention | Keep everything for now; periods and a delete-session job in Step 5 | Real periods are not agreed yet |
| Extra redaction rules | Standard secrets plus Aadhaar, PAN, phone, email only | No client-specific patterns yet; add as rules when known |

## Step 1: safe capture and ingestion

- [x] **1.1** Settings module (env-based config). *Done when:* one place reads config; tests cover defaults.
- [x] **1.2** Redaction module: secret rules, Aadhaar, PAN, phone, email; records rule and
  location, never the value. *Done when:* planted fake values are always replaced.
- [x] **1.3** Real migration `001`: session, session_env, event, redaction_log, job.
  *Done when:* applies and rolls back cleanly in Docker.
- [x] **1.4** MinIO storage wrapper (create bucket, put/get chunk). *Done when:* round-trip test passes.
- [x] **1.4a** Attachment de-duplication: store each attachment body once under its content
  hash (after redaction); the transcript line keeps a pointer. *Done when:* the same
  attachment uploaded in two sessions is stored once and the transcript can be rebuilt exactly.
- [x] **1.5** Ingestion API (FastAPI): health, token auth, chunk upload, session upsert,
  index job queued. *Done when:* uploading the same chunk twice stores one copy.
- [x] **1.6** Idle-session closer. *Done when:* a session is marked closed after N idle minutes.
- [x] **1.7** Capture client: incremental upload by byte offset, offline queue, retry.
  *Done when:* killing the API mid-session loses nothing once it returns.
- [x] **1.8** Environment and account record at session start. *Done when:* `session_env` has real values.
- [x] **1.9** Hook handlers for the hooks; SessionEnd as best-effort flush. *Done when:* real Claude
  Code captures a session through the hooks. (Private mode was split off: see 1.9b.)
- [ ] **1.9b** Private mode with logged toggles. **On hold by decision.** *Done when:* private mode
  sends only the minimal record.
- [x] **1.10** Code snapshots ported from the old proof of concept. *Done when:* the old checks pass
  (23 of the 27 apply and are ported one for one; 4 are superseded by design, see
  [snapshots.md](snapshots.md)).
- [ ] **1.10b** Fewer false positives in the snapshot secret scan (an entropy check on generic
  assignments, a per-repository allowlist). 6 % of this repository's own files were excluded.
  *Done when:* the false-positive rate on real repositories is acceptable without letting a hard-coded
  password through.
- [x] **1.11** Docs: setup, run and hook registration for Windows. *Done when:* a new developer can follow them cold. (Cold-start test by a fresh agent reached a captured session; its findings were fixed: module forms of every command, port changes, `CCP_STATE_DIR`, `started_at`, MinIO paths.)
- [ ] **1.12** Self-pilot for a week. *Done when:* zero lost sessions and no planted secret on the server.

**Gate:** 1.12 passes.

## Step 2: indexer (MinIO to PostgreSQL)

The index is **rebuildable**: everything in it comes from the stored transcripts, so it can be dropped and
rebuilt at any time. It runs on the server only; developer machines are not touched. Starts in parallel
with the pilot (1.12) and works on the sessions the pilot captures.

- [x] **2.1** Indexer core: a worker takes an `index_session` job, reads the session's stored chunks in
  order and writes one `event` row per transcript line (type, actor, uuid, parent, time, payload, source
  object and line). *Done when:* indexing a session twice gives identical rows, and a session still
  being uploaded can be indexed again later without duplicates. Includes the `thinking` table (text kept
  whenever Claude Code writes it; on 2.1.289 only a signature exists).
- [x] **2.2** Conversation tree and session facts: abandoned (rewound) branches, compaction records,
  `started_at`, `ended_at`, branch and repository filled on `session`. *Done when:* the verified facts in
  [transcript-facts.md](verify/transcript-facts.md) (rewind, compaction, resume) hold on test data.
- [x] **2.3** Model usage: model, tokens and cost per response. Cost is *read* from the transcript's
  `cost-state` entries, not computed (fact 13), so no price table. *Done when:* totals per session match
  the numbers in the transcript. (Matches on one of four real sessions with the main transcript only; the
  gaps are to be explained in 2.8, see [indexer.md](indexer.md).)
- [x] **2.4** Sub-agents: `agent_run` rows linked to the call that launched them, from the sub-agent
  streams. *Done when:* each sub-agent's instructions, result and events are reachable from the parent.
- [ ] **2.5** File changes: `file_change` rows from Edit and Write results (hunks, who, which event
  explains it). *Done when:* the history of one file lists every AI edit in order.
- [ ] **2.6** Test runs: recognise test commands and their pass or fail. *Done when:* common runners
  (pytest, npm test, dotnet test) are recognised on test data and unknown commands are ignored.
- [ ] **2.7** The six saved SQL questions as views or documented queries, and the docs for the indexer.
  *Done when:* each question has a test with known answers.
- [ ] **2.8** Rebuild and real-data check. *Done when:* dropping the index and rebuilding from MinIO gives
  identical rows, on the sessions captured during the pilot.

**Gate:** 2.8 passes.

## Later steps (summary)

- **Step 3:** dashboards, file history, "why did this line change", commit linking (demo 1).
- **Step 4:** distillation, pgvector search, MCP server, review step (demo 2).
- **Step 5:** move to a server, add developers, access roles, retention jobs.
