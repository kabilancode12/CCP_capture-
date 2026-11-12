# CCP: Claude Capture Platform

Captures every Claude Code session a developer runs, stores it safely, turns it into structured
data in PostgreSQL, and builds two outputs on top of it:

- **Audit trail**: what was done, by whom, with which model, and why.
- **Knowledge base**: how a problem was solved before, searchable by people and by Claude Code.

> **Status: Step 0 (verify and set up).** This repository contains the project foundation and the
> tools that establish what Claude Code actually writes to its transcripts. Capture, ingestion,
> the indexer and the knowledge layer are built in later steps. See [Roadmap](#roadmap).

## How it works

Three layers; each can be rebuilt from the one below it.

| Layer | Holds | Stored in |
| --- | --- | --- |
| 1. Raw capture | Session transcripts, code snapshots | MinIO (transcripts), Git refs (code) |
| 2. Index | Sessions, events, tool calls, file changes, model usage | PostgreSQL |
| 3. Knowledge | Distilled, reviewed answers with evidence | PostgreSQL + pgvector |

Details: [docs/architecture.md](docs/architecture.md).

## Quick start

Prerequisites: Python 3.11+, Git, Docker Desktop. Full instructions: [docs/setup.md](docs/setup.md).

```powershell
# 1. Python environment with development tools
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Local configuration (then edit .env and set real passwords)
Copy-Item .env.example .env

# 3. Start PostgreSQL and MinIO
docker compose --env-file .env -f infra/docker-compose.yml up -d

# 4. Check everything works
pytest
```

## Commands

| Task | Command |
| --- | --- |
| Inventory a transcript | `ccp-verify transcript <file-or-folder> [--out report.md]` |
| Describe this machine and surface | `ccp-verify env [--out env.json]` |
| Register the hook probe (scratch repos only) | `ccp-verify probe-settings` |
| See which hooks fired | `ccp-verify probe-report` |
| Apply the database schema | `ccp-db migrate` (see [docs/database.md](docs/database.md)) |
| Run tests | `pytest` |
| Lint and format check | `ruff check .` and `ruff format --check .` |
| Type check | `mypy` |
| Start / stop services | `docker compose --env-file .env -f infra/docker-compose.yml up -d` / `down` |

If a launcher such as `pytest.exe` is blocked on your machine, run it as a module
(`python -m pytest`, `python -m ccp.verify env`); see [docs/setup.md](docs/setup.md).

How to run the verification session and record the results: [docs/verify/](docs/verify/).

## Repository layout

```text
ccp_V2/
├── src/ccp/                 Application code (installable package)
│   ├── settings.py          Settings read from environment variables
│   ├── redaction/           Removes secrets and personal data before upload
│   ├── db/                  Database connection and migration runner
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
│   ├── architecture.md      Design and decisions
│   ├── setup.md             Installation and first run
│   ├── configuration.md     Every setting (environment variable) explained
│   ├── redaction.md         What is redacted, guarantees and limits
│   ├── database.md          Migrations: commands, writing and testing them
│   ├── verify/              Test-session script and transcript-facts template
│   └── reference/           Original proposal, handover, plan and diagrams
├── reference/poc/           The first proof of concept, kept unchanged for reference
├── pyproject.toml           Package metadata and tool settings (ruff, mypy, pytest)
└── .env.example             Configuration template (copy to .env)
```

## Roadmap

| Step | Content | Status |
| --- | --- | --- |
| 0 | Project setup, verification tools, local services | In progress |
| 1 | Capture hooks, redaction, incremental upload, ingestion API | Planned |
| 2 | Indexer and full event model in PostgreSQL | Planned |
| 3 | Dashboards, file history, commit linking | Planned |
| 4 | Knowledge layer: distillation, search, MCP server | Planned |
| 5 | Move to a server, add developers, harden | Planned |

## Security and privacy

Transcripts can contain secrets and personal data. Therefore:

- Real transcripts, `.env` files and storage data are git-ignored and must never be committed.
- Test data is written by hand; no real transcript is used in tests or documentation.
- The inspection tools print structure and counts, not raw content.
- Redaction before upload is built in Step 1, before any real capture begins.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding standards, commit conventions and the review
checklist. Changes are listed in [CHANGELOG.md](CHANGELOG.md).
