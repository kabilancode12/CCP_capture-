# Changelog

All notable changes are listed here. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

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
