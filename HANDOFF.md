# CCP handoff (written 2026-11-13)

Notes for continuing the Claude Capture Platform (CCP) on a new machine and in a new Claude Code session.
Delete this file once you are settled in. It is not part of the product documentation.

## What the project is

CCP captures every Claude Code session on developer machines (redacted transcripts, code snapshots,
environment record), stores it (MinIO for raw data, PostgreSQL for structured data), and builds an index, an audit
trail and dashboards on top. A knowledge base comes later. The old project (`claude-capture-project-files`, `docs`)
must stay unmodified. This repo is `ccp_V2`, branch `main`.

Working rules from the user: very structured, standard, simple, readable code; docs and comments on every file,
module and method; a proper README and setup docs; a code review will happen; ask if unclear. Commit trailer:
`Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## State

Steps 1 to 3.3 are built and committed. Last full run: `pytest -m "not slow"` gave 955 passed, ruff clean, mypy
strict clean. A real end-to-end run worked: a headless Claude Code session was captured, uploaded, indexed, audited
(`ccp-audit history`, `why`), rebuilt identically, commit-linked (100 % on a test commit), and shown on the Metabase
dashboard (16 cards).

Where things are (all under `src/ccp/`): `capture` (developer-machine client, hooks run `python -m ccp.capture hook`),
`api` (ingest), `index` (indexer, rebuild), `audit`, `dashboards`, `db` (migrations 001 to 013 in `db/migrations`),
`storage`, `settings.py`. Docs are in `docs/` (start with `docs/quickstart.md`; the task list is `docs/TODO.md`).

## Set up on the new machine

1. Clone the repo, install Python 3.12+ and Docker Desktop. Follow `docs/quickstart.md`: create a virtual
   environment, `pip install -e ".[dev]"`, copy `.env.example` to `.env` and fill it in, start the stack with
   `docker compose --env-file .env -f infra/docker-compose.yml up -d`, run `ccp-db migrate`.
2. If `.exe` launchers are blocked (Windows Application Control), use `python -m ccp.<name>` everywhere.
3. Check: `pytest -m "not slow"`, `ruff check`, `mypy`.
4. Dashboards (optional): set the dashboard block of `.env`, `ccp-db create-reader`, start Metabase with
   `--profile dashboards`, `ccp-dashboards setup`. The previous throwaway Metabase login
   (`admin@example.com` / `local-admin-pw-12345`) only exists in the old machine's Docker volume; choose your own.

## Next steps

- **1.12 self-pilot:** register the capture hooks in your own Claude Code settings (`docs/pilot.md`, the hook
  settings are printed by the capture client) and use Claude Code normally for about a week. Then tick 1.12 and 2.8
  (`ccp-index rebuild --all --check` on the pilot data) and open the dashboard on real data (3.3).
- **3.4:** shell-command and human changes from the code snapshots. Agree the approach with the user first.
- **1.10b:** fewer false positives in the snapshot secret scan (timing undecided).
- **1.9b private mode:** on hold by the user.
- Later: Step 4 (knowledge layer), Step 5 (server move, HTTPS and sign-in for dashboards, roles, retention,
  per-developer breakdown decision).

## Known open issues

- Summed `model_usage` can be higher than the transcript's `cost-state`: `cost-state` is a snapshot that lags (explained in `docs/indexer.md`).
- Plan-approval decisions in `v_human_decision` are untested on real transcripts.
- `repository` and `start_commit` are not filled; a job whose worker crashed stays `running`; indexing 5,000
  events takes about 18 s (one insert per event).
- Follow-ups: restrict access to `session_env`; tell developers what is collected before the pilot; Git notes for
  commit linking (needs a client hook); `run_mode` not collected; Linux and other surfaces untested.

## Design facts worth remembering

- Raw transcript lines are the source of truth; the PostgreSQL index is rebuildable
  (`ccp-index rebuild [--session ID | --all] [--check]`).
- One `event` row per transcript line; rewound prompts and their descendants are `is_abandoned`.
- Cost is read from `cost-state`, not computed. Dashboards read only `d_` views through the read-only `ccp_reader`
  role, so they can never show prompt text.
- An applied migration's checksum may not be edited; roll back and reapply instead.
- Gotchas: the pre-commit hook does not run mypy (run it yourself); in Bash, multiple heredocs in one command fail,
  so write files with the editor tools.

## Full history

The complete transcript of the previous session is at
`C:\Users\Xenovex\.claude\projects\c--workspace-claudeCapture\1528c746-71a7-445b-a3af-0e6848e8d842.jsonl`
(copy it to the new machine if you want the full detail; it is not needed to continue).
