# CCP: Claude Capture Platform

> The detailed documentation and the test suite are kept on the git branch `backup/full-2026-11-16`
> (`git show backup/full-2026-11-16:docs/<name>.md`).

Captures every Claude Code session a developer runs, stores it safely, turns it into structured
data in PostgreSQL, and builds two outputs on top of it:

- **Audit trail**: what was done, by whom, with which model, and why.
- **Knowledge base**: how a problem was solved before, searchable by people and by Claude Code.

> **Status: capture works end to end (Step 1 of 5).** Claude Code hooks upload each session's
> transcript, redacted on the developer's machine, to an ingestion API that stores it; code snapshots
> and an environment record go with it. The indexer, dashboards and knowledge layer (Steps 2 to 4)
> are not built yet. See the [roadmap](#roadmap).

## How it works

```text
Developer machine                        Server (local Docker for now)
-----------------                        -----------------------------
Claude Code --hooks--> capture client    Ingestion API --> MinIO       raw transcripts
                       redact, upload -->                \-> PostgreSQL sessions, environment, queue
                       code snapshots --> capture project (Git)        code states
```

Three layers; each can be rebuilt from the one below it.

| Layer | Holds | Stored in |
| --- | --- | --- |
| 1. Raw capture | Session transcripts, code snapshots | MinIO (transcripts), Git refs (code) |
| 2. Index | Sessions, events, tool calls, file changes, model usage | PostgreSQL |
| 3. Knowledge | Distilled, reviewed answers with evidence | PostgreSQL + pgvector |

Details: docs/architecture.md.

## Quick start

Prerequisites: Python 3.11+, Git, Docker Desktop. The full walk-through, with checks after every step, is
**`./run.sh`** (one command: checks the tools, writes `.env`, starts the database, object store,
dashboards and the CCP server, applies the migrations, and sets up capture in Claude Code; it asks only
for what must come from a person, such as the GitHub capture repo). Step by step: **docs/quickstart.md**. In short, on the server:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env          # then edit .env: passwords, and the token developers will use
docker compose --env-file .env -f infra/docker-compose.yml up -d
ccp-db migrate
ccp-ingest                           # leave running; check http://127.0.0.1:8000/health
```

and on each developer machine (no packages beyond Python):

```powershell
python -m venv $HOME\.ccp\venv
& $HOME\.ccp\venv\Scripts\pip install <path to this repository>
& $HOME\.ccp\venv\Scripts\python -m ccp.capture hook-settings --api-url http://<server>:8000
# merge the printed JSON into ~\.claude\settings.json and paste the token
```

Something not working? docs/troubleshooting.md.

## Documentation

| If you want to... | Read |
| --- | --- |
| Get it running | quickstart.md, then troubleshooting.md |
| Run the one-week self-pilot | pilot.md |
| Architecture diagram and the full status | [README-ARCHITECTURE-AND-STATUS.md](README-ARCHITECTURE-AND-STATUS.md) |
| What is built, and what is left | requirements.md |
| Ask questions of the captured data (cost, file history, sub-agents, ...) | queries.md, audit.md |
| See usage and cost on a dashboard | dashboards.md |
| Know every setting | configuration.md |
| Understand what is captured and how | hooks.md, capture-client.md, snapshots.md, environment.md |
| Understand privacy and safety | redaction.md (secrets and personal data), environment.md (personal data collected) |
| Understand the server | ingestion-api.md, indexer.md, storage.md, database.md |
| See the design and decisions | architecture.md, verify/transcript-facts.md |
| Work on the project | setup.md, CONTRIBUTING.md, docs/TODO.md |

## Commands

| Task | Command |
| --- | --- |
| Inventory a transcript | `ccp-verify transcript <file-or-folder> [--out report.md]` |
| Describe this machine and surface | `ccp-verify env [--out env.json]` |
| Register the hook probe (scratch repos only) | `ccp-verify probe-settings` |
| See which hooks fired | `ccp-verify probe-report` |
| Start the ingestion API | `ccp-ingest` (see docs/ingestion-api.md) |
| Close idle sessions once | `ccp-ingest close-idle` |
| Index stored transcripts into PostgreSQL | `ccp-index run` or `ccp-index run --once` (see docs/indexer.md) |
| Rebuild the index and prove it is identical | `ccp-index rebuild --all --check` |
| History of a file / which edit wrote a line | `ccp-audit history <file>` / `ccp-audit why <file> <line>` (see docs/audit.md) |
| Read-only role for dashboards / make the dashboard in Metabase | `ccp-db create-reader` / `ccp-dashboards setup` (see docs/dashboards.md) |
| Link commits to sessions / see the link of a commit | `ccp-audit link-commits --repo <git directory>` / `ccp-audit commit <sha>` |
| Take a code snapshot now (developer machine) | `ccp-capture snapshot --session-id <id>` (see docs/snapshots.md) |
| Print the Claude Code hook settings | `ccp-capture hook-settings` (see docs/hooks.md) |
| Record where a session runs (developer machine) | `ccp-capture environment --session-id <id>` (see docs/environment.md) |
| Upload a session (developer machine) | `ccp-capture sync --session-id <id> <transcript>` (see docs/capture-client.md) |
| Apply the database schema | `ccp-db migrate` (see docs/database.md) |
| Run tests | `pytest` (all, about 4 minutes) or `pytest -m "not slow"` (about 1 minute; skips the Git-heavy snapshot tests) |
| Lint and format check | `ruff check .` and `ruff format --check .` |
| Type check | `mypy` |
| Start / stop services | `docker compose --env-file .env -f infra/docker-compose.yml up -d` / `down` |

If a launcher such as `pytest.exe` is blocked on your machine, run it as a module
(`python -m pytest`, `python -m ccp.verify env`); see docs/setup.md.

How to run the verification session and record the results: docs/verify/.

## Repository layout

```text
ccp_V2/
├── src/ccp/                 Application code (installable package)
│   ├── settings.py          Settings read from environment variables
│   ├── redaction/           Removes secrets and personal data before upload
│   ├── db/                  Database connection and migration runner
│   ├── storage/             MinIO object store, key layout, attachment de-duplication
│   ├── ingest/              Ingestion API (Starlette): receives transcript chunks
│   ├── index/               Indexer: stored transcripts to events in PostgreSQL
│   ├── audit/               Audit commands: file history, which edit wrote a line, commit links
│   ├── dashboards/          Sets up the Metabase dashboard through its API
│   ├── capture/             Capture client: runs on developer machines, uploads transcripts
│   └── verify/              Step 0 tools: transcript, environment and hook inspectors
├── tests/
│   ├── unit/                Fast tests, one file per module
│   ├── integration/         Tests that need the local PostgreSQL (skipped if it is not running)
│   └── fixtures/            Hand-written synthetic data (never real transcripts)
├── infra/                   Docker Compose for local PostgreSQL and MinIO
├── db/
│   ├── migrations/          Numbered SQL schema files, applied by `ccp-db`
│   └── drafts/              Full planned schema (later steps)
├── docs/
│   ├── quickstart.md        From nothing to a captured session (server + developer machine)
│   ├── troubleshooting.md   Problems we have met, by symptom
│   ├── architecture.md      Design and decisions
│   ├── setup.md             Setting up a development environment
│   ├── TODO.md              The working task list
│   ├── configuration.md     Every setting (environment variable) explained
│   ├── redaction.md         What is redacted, guarantees and limits
│   ├── database.md          Migrations: commands, writing and testing them
│   ├── storage.md           Object storage, key layout, attachment de-duplication
│   ├── ingestion-api.md     Endpoints, status codes and how an upload is handled
│   ├── indexer.md           The indexer: how sessions become events, runs, changes and costs
│   ├── queries.md           The six saved questions (SQL views) and how to use them
│   ├── audit.md             ccp-audit: file history, "why was this line written", commit linking
│   ├── dashboards.md        Dashboards in Metabase: set up as code, read-only role
│   ├── capture-client.md    The developer-machine client: how a sync works and its promises
│   ├── environment.md       The environment record: fields, sources, privacy
│   ├── hooks.md             Claude Code hooks: what each event does, setup, safety rules
│   ├── snapshots.md         Code snapshots: hidden Git commits, safety, push queue, limits
│   ├── verify/              Test-session script and transcript-facts template
│   └── reference/           Original proposal, handover, plan and diagrams
├── reference/poc/           The first proof of concept, kept unchanged for reference
├── pyproject.toml           Package metadata and tool settings (ruff, mypy, pytest)
└── .env.example             Configuration template (copy to .env)
```

## Roadmap

| Step | Content | Status |
| --- | --- | --- |
| 0 | Project setup, verification tools, local services | Done |
| 1 | Capture hooks, redaction, incremental upload, ingestion API, code snapshots | Built; documentation and a week of own use remain (private mode is on hold) |
| 2 | Indexer and full event model in PostgreSQL | Planned |
| 3 | Dashboards, file history, commit linking | Planned |
| 4 | Knowledge layer: distillation, search, MCP server | Planned |
| 5 | Move to a server, add developers, harden | Planned |

## Security and privacy

Transcripts can contain secrets and personal data. Therefore:

- Secrets and personal data (Aadhaar, PAN, mobile numbers, e-mail addresses, keys, tokens) are
  **redacted on the developer's machine before anything is uploaded**: docs/redaction.md.
- Real transcripts, `.env` files and storage data are git-ignored and must never be committed.
- Test data is written by hand; no real transcript is used in tests or documentation.
- The inspection tools and the logs show structure, counts and reasons, never content or tokens.
- The environment record holds personal data (user, host and account names) by design; see
  docs/environment.md. Developers must be told what is captured before any pilot.

## Contributing

See CONTRIBUTING.md for coding standards, commit conventions and the review
checklist. Changes are listed in CHANGELOG.md.
