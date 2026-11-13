# Troubleshooting

Problems we have actually met, by where they show up. First, where the logs are.

## Where to look

| What | Where |
| --- | --- |
| What each hook did (developer machine) | `~/.ccp/capture/hook.log` (or `CCP_STATE_DIR`). One line per hook; no content, no token |
| The API's own output | The window running `ccp-ingest` |
| Whether the server is healthy | `curl.exe http://127.0.0.1:8000/health` |
| The state of the containers | `docker compose --env-file .env -f infra/docker-compose.yml ps` and `... logs postgres` / `... logs minio` |
| Which hooks fire at all | `ccp-verify probe-settings` / `probe-report` ([verify/](verify/test-session-script.md)) |

## Installing and running commands

| Symptom | Cause and fix |
| --- | --- |
| `ccp-db` (or any `ccp-...`) "is not recognized" | The virtual environment is not active in this window. Run `.\.venv\Scripts\Activate.ps1`, or call it as a module with the environment's Python: `python -m ccp.db`, `python -m ccp.capture`, `python -m ccp.verify` |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | Once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `python3: Python was not found` or the Microsoft Store opens | The Store shortcut is on the PATH. Use `python`, or switch the shortcut off in Settings > Apps > Advanced app settings > App execution aliases |
| `pytest.exe` / `mypy.exe` "blocked by an Application Control policy" | Company policy blocks unsigned launchers. Run them as modules: `python -m pytest`, `python -m mypy` |
| `mypy` fails with "DLL load failed ... Application Control policy" | The prebuilt mypy has a compiled file that is blocked. Install the pure-Python build: `pip install --force-reinstall --no-deps --no-binary mypy mypy` |
| An import fails with "DLL load failed ... Application Control policy" for another package | The package has a compiled part that company policy blocks. This project uses pure-Python packages on purpose (`pg8000`, `starlette`, `minio`); do not swap in `psycopg[binary]` or `pydantic`/FastAPI |

## Docker, PostgreSQL and MinIO

| Symptom | Cause and fix |
| --- | --- |
| `docker info` says "unable to start" | Start Docker Desktop; make sure WSL 2 or Hyper-V is enabled |
| `docker compose` says a variable is not set | Run it from the repository folder with `--env-file .env` |
| "Port is already allocated" | Something else uses the port. Change `POSTGRES_PORT`, `MINIO_API_PORT` or `MINIO_CONSOLE_PORT` in `.env` (and `MINIO_ENDPOINT` to match) |
| Every database or storage call takes about 2 seconds | The host is `localhost`. On Windows it tries IPv6 first and Docker listens on IPv4 only. Use `127.0.0.1` in `POSTGRES_HOST` and `MINIO_ENDPOINT` |
| `ccp-ingest` stops with `Storage request failed (InvalidAccessKeyId)` or `(SignatureDoesNotMatch)` | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` differ from `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`. Make each pair equal. If you changed the root password **after** the first start, MinIO still has the old one: either use the old value or start clean with `down -v` (this deletes stored data) |
| PostgreSQL rejects the password after you changed `POSTGRES_PASSWORD` | The database was created with the old password and keeps it. Use the old value, or start clean with `down -v` (deletes data) |
| Nothing at `http://127.0.0.1:9001` | MinIO is not up: `docker compose ... ps`, then `docker compose ... logs minio` |

## The database and the API

| Symptom | Cause and fix |
| --- | --- |
| `ccp-ingest` prints "The database schema is behind this server (pending: ...). Run: ccp-db migrate" | Exactly that: run `ccp-db migrate`. Do it after every update of the code |
| `ccp-ingest` or `ccp-db` prints `Invalid settings:` and a list | The list names each missing or invalid variable (never its value). Fix `.env` |
| `ccp-db` says `X was changed after it was applied` | A migration file that was already applied has been edited. Restore it from Git; to change the schema, add a new migration ([database.md](database.md)) |
| `/health` says `degraded` | The answer names the failing part (`database` or `storage`). Check the containers and the credentials above |
| Hooks log `server error (503)` | The API reached the database or storage but they failed. The client keeps its position and retries on the next hook: nothing is lost. Look at the API window |

## Hooks on the developer machine

| Symptom | Cause and fix |
| --- | --- |
| `hook.log` does not exist | The hooks never ran. In Claude Code run `/hooks`: the six capture hooks should be listed. Check the JSON in `settings.json` is valid and was merged under `hooks`. A hook command that points to the wrong Python path fails silently: it must be `"<full path>/python.exe" -m ccp.capture hook` |
| `settings invalid: ... CCP_INGEST_TOKEN is required` | The `env` block is missing in `settings.json`, or the token is shorter than 16 characters |
| `rejected (401)` | The token in `settings.json` differs from the server's `CCP_INGEST_TOKEN`, or still is `REPLACE_WITH_THE_INGEST_TOKEN` |
| `the API is unavailable; will retry on the next sync` | The server is not running, or `CCP_API_URL` is wrong, or a firewall blocks the port. Nothing is lost: the next hook retries from the same place |
| `no transcript yet` on the first events | Normal. Claude Code creates the transcript file only after the first response; `Stop` uploads it |
| `skipped: internal helper (no agent type)` after every response | Normal. Claude Code fires `SubagentStop` for an internal helper; it has no transcript to upload |
| The last lines of a session are missing | The `SessionEnd` background flush is best effort. The next `SessionStart` uploads what is left; start Claude Code again on that machine |
| Claude Code shows hook errors or is slower | A hook that fails must not show an error: check `hook.log`. Every hook has a time limit (5 to 30 seconds); the log line ends with the time it took |
| Nothing is printed by the hooks | That is by design: a `SessionStart` hook's output would be added to the conversation |

## Code snapshots

| Symptom | Cause and fix |
| --- | --- |
| No `refs/claude-capture` refs anywhere | Snapshots are off until `CCP_SNAPSHOT_REMOTE`, `CCP_SNAPSHOT_REMOTE_BASE` or `CCP_SNAPSHOTS=true` is set in the `env` block |
| The log says `kept locally (no capture project configured)` | `CCP_SNAPSHOTS=true` without a remote: the refs are in the developer's own repository. Set a remote to push them |
| The log says `push failed, queued for the next session start` | The capture project does not exist or is unreachable. A local one is created with `git init --bare <folder>`. The queue is retried at the next SessionStart |
| The log says `kept local (a secret is in unpushed commits)` | A secret pattern was found in local commits that exist nowhere else; a snapshot is pushed together with the commits it builds on, so it stays local. Remove the secret from the history, or push the commits |
| `N file(s) excluded: secret` | Files that look like they hold a secret are left out of snapshots (their names are in the snapshot's commit message). Some are false positives, for example a placeholder such as `TOKEN=change-me-...`; see the limits in [snapshots.md](snapshots.md) |
| Each response adds about a second | Taking a snapshot starts about eight Git processes, which is slow on Windows. This is measured and documented in [snapshots.md](snapshots.md) |

## Development

| Symptom | Cause and fix |
| --- | --- |
| `git commit` is rejected by "ruff format" (it reformatted files) | That is the formatter doing its job: `git add -A` and commit again. If it keeps happening, your local ruff differs from the pinned one: `pip install -e ".[dev]"` again |
| `git commit` fails at "gitleaks" | Docker Desktop must be running; or a real secret was staged. The report names the file and line |
| The test suite takes minutes | The snapshot tests start many Git processes. `pytest -m "not slow"` takes about a minute |
| Integration tests are skipped | PostgreSQL or MinIO is not reachable. Start the containers (part A3) |

## Starting over

```powershell
docker compose --env-file .env -f infra/docker-compose.yml down -v   # deletes all stored data
Remove-Item -Recurse $HOME\.ccp\capture                              # the client's position and log
```

Then repeat the quick start from A3. (The client's position is what tells it how much it has uploaded; if
you wipe the server's data but keep the client's position, old lines are not sent again. Wipe both.)
