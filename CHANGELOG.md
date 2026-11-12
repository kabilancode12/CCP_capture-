# Changelog

All notable changes are listed here. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Changed

- The chunk upload response no longer contains `next_offset` (the uploaded text is redacted, so its
  size is not the source position).
- The redactor writes redacted lines in Claude Code's compact JSON format, so redacted attachment
  lines can still be de-duplicated.

### Fixed

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
