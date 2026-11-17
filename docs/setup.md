# Setting up a development environment (Windows)

This page is for people who **work on the project itself**: editing code, running tests, committing.
To simply run the system, follow [quickstart.md](quickstart.md). Commands are for PowerShell.

## 1. Prerequisites

| Tool | Check | Notes |
| --- | --- | --- |
| Python 3.11 or newer | `python --version` | Windows Store stubs do not count; install from python.org |
| Git | `git --version` | Git for Windows includes Git Bash |
| Docker Desktop | `docker info` | Must say "Server Version"; if not, start Docker Desktop. Needed for the integration tests and for the secret scan when you commit |
| Claude Code | `claude --version` | Only to produce real transcripts or to try the hooks |

## 2. Python environment

```powershell
cd <repository root>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

`-e` installs the package in editable mode, so code changes take effect immediately. `[dev]` includes the
server's packages and the test and lint tools. If PowerShell blocks the activate script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

## 3. Services and configuration

The integration tests use the real PostgreSQL and MinIO, each in a throwaway database or bucket. Set them
up exactly as in [quickstart.md](quickstart.md), parts A2 to A4: copy `.env.example` to `.env`, edit it,
start the containers and run `ccp-db migrate`. Every variable is explained in
[configuration.md](configuration.md). Without the containers the integration tests are skipped, not failed.

| Service | Address (this machine only) | Sign-in |
| --- | --- | --- |
| PostgreSQL | `127.0.0.1:5432` | `POSTGRES_USER` / `POSTGRES_PASSWORD` from `.env` |
| MinIO API | `127.0.0.1:9000` | |
| MinIO console | <http://127.0.0.1:9001> | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` |

Use `127.0.0.1`, not `localhost` (see [troubleshooting.md](troubleshooting.md)). A database shell, without
installing `psql`:

```powershell
docker compose --env-file .env -f infra/docker-compose.yml exec postgres psql -U ccp -d ccp
```

MinIO's official images are no longer published, so the Compose file uses Chainguard's MinIO build, pinned by
digest (see the comments in `infra/docker-compose.yml`).

## 4. Pre-commit checks

```powershell
python -m pre_commit install
python -m pre_commit run --all-files    # optional: check everything now
```

The checks are whitespace and file hygiene, ruff (lint and format) and a gitleaks secret scan of staged
changes. Docker Desktop must be running when you commit. The old-project copies in `reference/` and
`docs/reference/` are excluded and never modified. To scan the whole Git history for secrets:

```powershell
docker run --rm -v "${PWD}:/repo" -w /repo zricethezav/gitleaks:v8.21.2 detect --source . --redact
```

## 5. Verify the setup

```powershell
pytest -m "not slow"    # about a minute; plain `pytest` runs everything (about four minutes)
ruff check .            # no lint problems
ruff format --check .   # nothing to reformat
mypy                    # no type errors
```

If your machine blocks the `.exe` launchers (see [troubleshooting.md](troubleshooting.md)), use the module
form: `python -m pytest`, `python -m ruff check .`, `python -m mypy`.

## Where things are

The repository layout, and what each document covers, are in the [README](../README.md). The working task
list is [TODO.md](TODO.md); coding standards and the review checklist are in
[../CONTRIBUTING.md](../CONTRIBUTING.md).
