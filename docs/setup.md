# Setup guide (Windows)

How to get a working local development environment. Commands are for PowerShell unless noted.

## 1. Prerequisites

| Tool | Check | Notes |
| --- | --- | --- |
| Python 3.11 or newer | `python --version` | Windows Store stubs do not count; install from python.org |
| Git | `git --version` | Git for Windows includes Git Bash |
| Docker Desktop | `docker info` | Must say "Server Version"; if not, start Docker Desktop |
| Claude Code | `claude --version` | Needed only to produce real transcripts for Step 0 |

Secret scanning with gitleaks runs through Docker (no separate install). Docker Desktop must be
running when you commit.

## 2. Python environment

```powershell
cd <repository root>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

`-e` installs the package in editable mode, so code changes take effect immediately.
If PowerShell blocks the activate script, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

Enable the pre-commit checks so problems are caught before a commit:

```powershell
python -m pre_commit install
python -m pre_commit run --all-files    # optional: check everything now
```

The checks are whitespace and file hygiene, ruff, and a gitleaks secret scan of staged changes.
The old-project copies in `reference/` and `docs/reference/` are excluded and never modified.
To scan the whole Git history for secrets:
`docker run --rm -v "${PWD}:/repo" -w /repo zricethezav/gitleaks:v8.21.2 detect --source . --redact`.

## 3. Configuration

```powershell
Copy-Item .env.example .env
```

Open `.env` and replace both `change-me-...` passwords. `.env` is git-ignored. Never commit it.

## 4. Start the local services

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
```

Expected: `postgres` shows `healthy` and `minio` shows `Up`. (The MinIO image has no shell, so it
has no health check. Check it with `curl.exe http://127.0.0.1:9000/minio/health/ready`, which
should return HTTP 200.) The raw-transcript bucket (`MINIO_BUCKET`) is created by the
application in Step 1; until then, create it by hand in the MinIO console if you need it.

MinIO's official images are no longer published, so the Compose file uses Chainguard's MinIO
build, pinned by digest. Details are in the comments in `infra/docker-compose.yml`.

| Service | Address (this machine only) | Sign-in |
| --- | --- | --- |
| PostgreSQL | `localhost:5432` | `POSTGRES_USER` / `POSTGRES_PASSWORD` from `.env` |
| MinIO API | `localhost:9000` | |
| MinIO console | <http://localhost:9001> | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` |

Open a database shell (no local `psql` needed):

```powershell
docker compose --env-file .env -f infra/docker-compose.yml exec postgres psql -U ccp -d ccp
```

Stop the services with `down`. Add `-v` to also delete all stored data.

## 5. Verify the setup

```powershell
pytest              # all tests pass
ruff check .        # no lint problems
mypy                # no type errors
ccp-verify env      # prints a JSON description of this machine
```

If your machine blocks the `.exe` launchers (see Troubleshooting), use the module form instead:
`python -m pytest`, `python -m ruff check .`, `python -m mypy`, `python -m ccp.verify env`.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| `docker info` says "unable to start" | Start Docker Desktop; make sure WSL 2 or Hyper-V is enabled |
| Port already in use | Change `POSTGRES_PORT`, `MINIO_API_PORT` or `MINIO_CONSOLE_PORT` in `.env` |
| `python3: Python was not found` | The Microsoft Store stub is on PATH; use `python`, or turn off the stub in Settings > Apps > Advanced app settings > App execution aliases |
| Compose says a variable is not set | Run compose with `--env-file .env` from the repository root |
| `pytest.exe` or `mypy.exe` "blocked by an Application Control policy" | Company policy blocks unsigned launchers. Run them as modules: `python -m pytest`, `python -m ruff check .`, `python -m ccp.verify env` |
| pre-commit fails with "Application Control policy has blocked" while building a hook | Hooks that need a compiler or Go toolchain are blocked. This repository avoids them (gitleaks runs as a Docker image); do not add hooks that build from source |
| `mypy` fails with "DLL load failed ... Application Control policy" | The prebuilt mypy contains a compiled file that is blocked. Install the pure-Python build: `pip install --force-reinstall --no-deps --no-binary mypy mypy` |
