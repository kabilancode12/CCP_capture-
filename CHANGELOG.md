# Changelog

All notable changes are listed here. Format based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- Project foundation: `src/` package layout, `pyproject.toml`, ruff, mypy, pytest, pre-commit.
- `ccp-verify transcript`: inventory of Claude Code transcripts (entry types, thinking, edit
  results, conversation tree, compaction, sub-agents), with values masked.
- `ccp-verify env`: description of the machine, surface, shell and account storage, with
  values hidden.
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
