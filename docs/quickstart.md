# Quick start: from nothing to a captured session

This page takes you from a fresh checkout to a Claude Code session that shows up in the database. It is
written for Windows and PowerShell. It has three parts:

- **A. The server**: PostgreSQL, MinIO and the ingestion API. For now they run on one machine.
- **B. A developer machine**: install the small client and register the hooks.
- **C. Check that it worked.**

If something does not behave as written, look in [troubleshooting.md](troubleshooting.md) first.
Every setting is explained in [configuration.md](configuration.md).

## What you need

| Tool | Check it with | Notes |
| --- | --- | --- |
| Python 3.11 or newer | `python --version` | From python.org. The Microsoft Store shortcut does not count |
| Git | `git --version` | Git for Windows (it includes Git Bash, which Claude Code uses for hooks) |
| Docker Desktop | `docker info` | Must print a "Server Version"; if not, start Docker Desktop |
| Claude Code | `claude --version` | On the developer machine (part B) |

---

## A. The server

Do this once, in the repository folder.

### A1. Install

```powershell
cd <the repository folder>
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

`[dev]` includes everything the server needs plus the test tools. (A server that will not be developed on
can use `".[server]"` instead.) If PowerShell refuses to run the activate script:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then try again.

Check: `ccp-db --help` prints usage. If it says the command is not recognised, the virtual environment is not
active in this window: run the `Activate.ps1` line again.

**If your machine blocks `.exe` launchers** ("blocked by an Application Control policy"), every command in
this guide has a module form that needs no launcher. Use `python -m pip` as above, and:

| Launcher | Module form |
| --- | --- |
| `ccp-db` | `python -m ccp.db` |
| `ccp-ingest` | `python -m ccp.ingest` |
| `ccp-capture` | `python -m ccp.capture` |
| `ccp-verify` | `python -m ccp.verify` |

### A2. Configure

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace the three placeholders, and **keep these pairs equal**, because they describe the same thing for two
programs (Docker creates MinIO with the first of each pair; the application signs in with the second):

| Set this | And this to the same value | Rule |
| --- | --- | --- |
| `MINIO_ROOT_USER` | `MINIO_ACCESS_KEY` | |
| `MINIO_ROOT_PASSWORD` | `MINIO_SECRET_KEY` | at least 8 characters |
| `POSTGRES_PASSWORD` | | any value |
| `CCP_INGEST_TOKEN` | | at least 16 characters; developers need it in part B |

Keep `127.0.0.1` as the host (not `localhost`; see troubleshooting). `.env` is ignored by Git: never commit it.

The commands below use the default ports (PostgreSQL 5432, MinIO 9000 and 9001, API 8000). If one is taken,
change `POSTGRES_PORT`, `MINIO_API_PORT`, `MINIO_CONSOLE_PORT` or `CCP_API_PORT` in `.env`, set
`MINIO_ENDPOINT` to match, and use the new port in the URLs below.

### A3. Start PostgreSQL and MinIO

```powershell
docker compose --env-file .env -f infra/docker-compose.yml up -d
docker compose --env-file .env -f infra/docker-compose.yml ps
```

The first start downloads the images. When it is ready, `postgres` says `healthy` and `minio` says `Up`.
Check MinIO: `curl.exe http://127.0.0.1:9000/minio/health/ready` should answer with HTTP 200 (no text).

### A4. Create the database tables

```powershell
ccp-db migrate
ccp-db status
```

`status` must show `[x]` next to every migration. Run `ccp-db migrate` again after every update of the code:
the server refuses to start while migrations are pending.

### A5. Start the ingestion API

```powershell
ccp-ingest
```

Leave this window open: the server runs in it (stop it with Ctrl+C). In a **second** PowerShell window:

```powershell
curl.exe http://127.0.0.1:8000/health
```

Expected: `{"status":"ok","database":"ok","storage":"ok"}`. The first start creates the MinIO bucket by itself.
If it says `degraded`, the name of the failing part is in the answer.

The server is done. It also closes sessions that have been quiet for 30 minutes (see
[ingestion-api.md](ingestion-api.md)).

---

## B. A developer machine

Do this on each machine whose Claude Code sessions should be captured. It needs **no packages beyond
Python** and no database or storage credentials: the client talks only to the API.

### B1. Install the client in its own environment

```powershell
python -m venv $HOME\.ccp\venv
& $HOME\.ccp\venv\Scripts\python -m pip install <path to the repository folder>
& $HOME\.ccp\venv\Scripts\python -m ccp.capture hook-settings --api-url http://127.0.0.1:8000
```

Use the address of the machine that runs the server instead of `127.0.0.1` when it is another machine.
The last command prints a JSON block with an `env` part and a `hooks` part. A dedicated environment keeps the
path in the hook command stable.

### B2. Register the hooks with Claude Code

Open `~\.claude\settings.json` (create it if it does not exist; it must be one JSON object). Merge the printed
block into it:

- add the `env` entries to an existing `env` object, or add the whole `env` object;
- add the eight events under `hooks`, next to any hooks you already have;
- replace `REPLACE_WITH_THE_INGEST_TOKEN` with the real `CCP_INGEST_TOKEN` from part A2.

Keep the file valid JSON. (To try it on one project first, put the same content in that project's
`.claude/settings.local.json` instead.) Settings are read when Claude Code starts, so sessions that were
already open do not capture anything: start a new one. To keep the client's log and position somewhere other
than `$HOME\.ccp\capture`, add `"CCP_STATE_DIR": "<folder>"` to the `env` block.

### B3. See it work

Start a new Claude Code session in any Git repository and ask it something. Then:

```powershell
Get-Content $HOME\.ccp\capture\hook.log -Tail 20
```

You should see one line per hook, for example
`Stop session=... 1 chunk(s), 3 attachment(s) uploaded; environment: sent (0.7s)`. Inside Claude Code, `/hooks`
lists the registered hooks. If the log says `settings invalid`, `401` or `unavailable`, see
[troubleshooting.md](troubleshooting.md).

### B4. Optional: capture the code too

Code snapshots are off by default. To turn them on, give them a capture project: a separate, empty Git
repository (for now a local one will do) and its address in the same `env` block:

```powershell
git init --bare $HOME\.ccp\capture-projects\my-repo-capture.git
```

```json
"CCP_SNAPSHOT_REMOTE": "C:/Users/<you>/.ccp/capture-projects/my-repo-capture.git"
```

After the next response, `git for-each-ref refs/claude-capture` in that repository lists the snapshots. Details,
safety and limits: [snapshots.md](snapshots.md).

---

## C. Check that it worked

On the server machine, in the repository folder:

```powershell
docker compose --env-file .env -f infra/docker-compose.yml exec postgres psql -U ccp -d ccp -c "select session_id, status, started_at from session"
docker compose --env-file .env -f infra/docker-compose.yml exec postgres psql -U ccp -d ccp -c "select account_email is not null as has_account, surface, claude_code_version from session_env"
docker compose --env-file .env -f infra/docker-compose.yml exec postgres psql -U ccp -d ccp -c "select rule_id, sum(occurrences) from redaction_log group by 1"
```

You should see your session (`open` until it has been quiet for 30 minutes), its environment record, and the
redactions that were made (your own e-mail address is one). `started_at` is empty for now: the indexer
(Step 2) fills it from the transcript. The uploaded transcript is in the MinIO console at
<http://127.0.0.1:9001> (sign in with `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`), in bucket `ccp-raw`, under
`sessions/<session id>/main/` (one object per uploaded chunk; files that were pasted or attached are stored
once each under `attachments/`). Every line there has already been redacted on the developer machine.

## Stopping and starting again

| To | Do |
| --- | --- |
| Stop the API | Ctrl+C in its window |
| Stop PostgreSQL and MinIO (keeping the data) | `docker compose --env-file .env -f infra/docker-compose.yml down` |
| Start again | `docker compose ... up -d`, then `ccp-db migrate`, then `ccp-ingest` |
| Delete all stored data and start clean | `docker compose --env-file .env -f infra/docker-compose.yml down -v` |

## What next

- What each hook does and the rules it obeys: [hooks.md](hooks.md)
- What is redacted, and the limits: [redaction.md](redaction.md)
- How the pieces fit together: [architecture.md](architecture.md)
- Developing the project itself: [setup.md](setup.md) and [../CONTRIBUTING.md](../CONTRIBUTING.md)
