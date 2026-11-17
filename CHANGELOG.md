# Changelog

All notable changes are listed here. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- Single-file chat report: `report.md` in every catalog folder, `ccp-catalog export --chat <id> [--out file]`, and a
  download link on each Explorer chat page. All catalog data as readable Markdown, nothing cut, with a
  completeness test (see `docs/catalog.md`). It is rewritten after every turn (a light build that leaves the raw
  transcript copy alone) and after a person reviews a decision.
- Chat hierarchy (migration 016): Claude account (`claude_account`) -> project (`project`, keyed by the
  normalised Git remote or the folder) -> chat (`session`, view `chat`) -> sessions of a chat (`chat_session`),
  plus the tables later steps fill (`test_case`, `correction`, `decision`, `ui_user`, `ui_access_log`) and
  the views `d_chat`, `v_slash_command` and `v_workflow_run`. Labels move from `account_label` and
  `project_label` into `claude_account.label` and `project.label`; `ccp-db label project` accepts a project
  name, key or folder; a folder labelled before any chat ran in it keeps its label when the first chat
  arrives, and a folder project (with its chats and label) is merged into the Git project when the folder
  gets a Git remote, so one repository is one project. A home folder is shown on dashboards as a neutral
  `project-<hash>`, never by its last part (the login name). Decision evidence points at raw transcript lines (`decision.evidence_lines`), not event ids, so it
  survives a rebuild. Windows folders are keyed with a lower-case drive letter and backslashes.
  **After migrating, run `ccp-db create-reader` and `ccp-db create-auditor` again.** The migration keeps the
  existing right to read `d_usage` and gives the same roles `d_chat`, but only re-running the commands brings
  the roles exactly up to date (the auditor needs them for the new tables and views).
- Capture client: the environment record adds the account uuid, full name and role, a hash of the machine id,
  the folder, the Git remote (without credentials) and the Git identity; SessionStart and SessionEnd report
  each session of a chat (`POST /v1/sessions/{id}/lifecycle`); tool-result files are redacted and uploaded
  (`PUT /v1/sessions/{id}/tool-results/{name}`; a file holding JSON is redacted value by value, as transcript
  lines are; a changed file is stored as a new version next to the first, never over it); Workflow sub-agent
  descriptions carry their run id; and `CCP_CAPTURE_DISABLED=1` switches the hooks off. All of it works
  against an older server (logged once); an environment record that an older server took only in part is
  sent whole again an hour later. SessionEnd no longer waits for Git: the background flush adds the end
  commit, and it takes no final code snapshot for a chat without a transcript. The hooks' API client fails
  fast: once a request could not reach the API, the rest of that hook run does not wait for more timeouts
  (SessionStart queues its start for the catch-up instead of sending it on its own); a refused token stops a
  catch-up at once; the prompt hook leaves tool-result files for `Stop`. A start that arrives late
  no longer reopens an ended chat, and a rebuild keeps the start time of a chat without a transcript.

- Dashboards: five content-free dashboard views (migration 013), `ccp-db create-reader` (a read-only role that
  can read only those views), an optional Metabase service in the Compose file (`--profile dashboards`) and
  `ccp-dashboards setup`, which makes the 16-card dashboard through Metabase's API, repeatably (Step 3, task 3.3).
- Commit linking: `ccp-audit link-commits`, `commit` and `session-commits` link commits to the sessions
  whose recorded edits wrote their lines, with a confidence (`commit_link`, migration 012; Step 3, task 3.2).
- `ccp-audit history <file>` and `ccp-audit why <file> <line>`: the history of a file, and which AI edit
  (with its prompt) wrote a line, found by replaying the recorded line ranges (Step 3, task 3.1).
- The indexer (`ccp-index run [--once]`): turns stored transcripts into one `event` row per line, safe to
  repeat, with a retrying job queue. Migration 004 adds the `thinking` table (task 2.1).
- Indexer: abandoned (rewound) branches are marked, compactions get their own table (migration 005), and
  `session.started_at` and `branch` are filled from the transcript (task 2.2).
- Indexer: model usage per response (de-duplicated by message id) and session cost per model, read from
  the transcript's `cost-state` (migration 006, task 2.3).
- Indexer: sub-agent runs (`agent_run`, migration 007, task 2.4). A sub-agent's description file is no longer
  stored as an event.
- Indexer: file changes from Edit and Write results, with line ranges and the call and prompt behind
  each (`file_change`, migration 008), and an indexed `event.tool_use_ids` (migration 009) (task 2.5).

### Changed

- Quick start and troubleshooting now give the `python -m` form of every command (for machines that
  block `.exe` launchers), explain changing ports and `CCP_STATE_DIR`, and describe what part C shows.
- Dependencies are split: the capture client needs no packages; the server's are in the `server`
  extra. Importing the client no longer loads MinIO (import time 0.46 s to 0.15 s).
- The chunk upload response no longer contains `next_offset` (the uploaded text is redacted, so its
  size is not the source position).
- The redactor writes redacted lines in Claude Code's compact JSON format, so redacted attachment
  lines can still be de-duplicated.

### Fixed

- Data sent to Git on Windows had `
` turned into `
` (broke branch backups, put carriage returns into
  snapshot messages); it is now sent as bytes.
- Every hook log line was written twice (the handler was attached to a logger and its parent).
- The API answered 401/400 without reading the request body, which could reset the connection on
  Windows: the client saw a network error instead of the refusal. The body is now read first.
- A redaction location was built from the original dictionary key, so a key that was itself a secret
  could be copied into the redaction log.

### Added

- Project foundation: `src/` package layout, `pyproject.toml`, ruff, mypy, pytest, pre-commit.
- `ccp-verify transcript`: inventory of Claude Code transcripts (entry types, thinking, edit
  results, conversation tree, compaction, sub-agents), with values masked.
- `ccp-verify env`: description of the machine, surface, shell and account storage, with
  values hidden.
- Code snapshots (`ccp.capture.snapshots`, `ccp-capture snapshot|flush`): the working tree as hidden Git
  commits after each response, built in a temporary index so the developer's repository is never
  touched; files with secrets excluded (named in the commit message); pushed to a capture project with a
  retry queue; opt-in. Ported the first project's checks (23 of 27 apply). Measured 2.4 s / 1.0 s on a
  151-file repository.
- Claude Code hooks (`ccp-capture hook`, `hook-settings`): SessionStart, UserPromptSubmit, Stop,
  SubagentStop, PreCompact and SessionEnd upload what is new. Always exit 0, print nothing, log no
  content, short upload budgets, internal-helper SubagentStop skipped, detached best-effort flush at
  SessionEnd, catch-up at SessionStart. Verified with real Claude Code. Private mode is deferred.
- Server start-up schema check: refuses to start while migrations are pending.
- Environment record (`ccp-capture environment`, `PUT /v1/sessions/{id}/environment`): version,
  surface, shells, OS, host and user, and the account are collected in two stages (process at
  session start, transcript once it exists) and merged on the server. Personal data: see
  docs/environment.md.
- Capture client (`ccp-capture sync|catch-up`): reads new complete transcript lines, redacts,
  moves large attachments out, uploads chunks, attachments and redaction reports through the API,
  and saves its position only after success. Handles sub-agent files; per-stream locks; time
  budget. Nothing lost when the API is down, nothing duplicated on retry.
- `POST /v1/sessions/{id}/redactions` and migration 003 (idempotent `redaction_log`).
- `PUT /v1/attachments/{digest}`: developers' machines upload de-duplicated attachment bodies
  through the API (the server verifies the SHA-256), so they need no storage credentials.
- Idle-session closer: open sessions with no activity for `CCP_SESSION_IDLE_MINUTES` are closed
  (end reason `idle_timeout`) with a final index job; runs in the server and as `ccp-ingest
  close-idle`; a late chunk reopens the session.
- Ingestion API (`ccp-ingest`, Starlette + uvicorn): `GET /health` and an idempotent
  `PUT .../chunks/{offset}` with bearer-token auth, validation, size limit, conflict detection,
  session upsert and one queued index job per session. Migration 002 adds session activity.
- Object storage (`ccp.storage`): MinIO wrapper, validated key layout with idempotent chunk keys,
  and attachment de-duplication by content hash with byte-exact restore. Uses the `minio` client.
- Database migrations (`ccp-db status|migrate|rollback`): numbered SQL files with checksums,
  per-migration transactions and a lock; first schema `001_initial_schema` (session, session_env,
  event, redaction_log, job). Uses the pure-Python `pg8000` driver.
- Redaction (`ccp.redaction`): 17 rules for secrets and Indian personal data (Aadhaar with
  checksum, PAN, mobile, email); records rule and location, never the value; fails closed.
- Settings module (`ccp.settings`): typed, validated configuration from environment variables
  and an optional `.env` file; reports all problems at once, never prints values, wraps secrets.
- Hook probe (`ccp-verify hook-probe`, `probe-settings`, `probe-report`): records which Claude
  Code hooks fire and their payload key names, never content; always exits 0.
- Local Docker Compose stack: PostgreSQL 16 with pgvector, and MinIO (Chainguard build pinned
  by digest, because MinIO no longer publishes official images).
- Draft database schema in `db/drafts/001_init.sql` (not applied).
- Documentation: README, setup guide, architecture summary, test-session script and
  transcript-facts template.
- Earlier proof of concept and planning documents copied in as reference.
