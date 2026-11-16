# Configuration

All settings are environment variables, read by [`src/ccp/settings.py`](../src/ccp/settings.py).
For local development put them in a git-ignored `.env` file (start from
[`.env.example`](../.env.example)). Real environment variables take precedence over the file.

On a server, set the variables in the service's environment; do not use a `.env` file there.

## Variables

"Required" means the application refuses to start without it. Secrets are never printed:
error messages name the variable, not its value.

### PostgreSQL

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `POSTGRES_HOST` | no | `127.0.0.1` | Database host. Use `127.0.0.1`, not `localhost`: on Windows `localhost` tries IPv6 first and adds about 2 s to every connection |
| `POSTGRES_PORT` | no | `5432` | Database port (1-65535) |
| `POSTGRES_DB` | **yes** | | Database name |
| `POSTGRES_USER` | **yes** | | Database user |
| `POSTGRES_PASSWORD` | **yes** (secret) | | Database password |

### MinIO (raw transcript storage)

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `MINIO_ENDPOINT` | no | `127.0.0.1:9000` | `host:port`, without `http://` |
| `MINIO_ACCESS_KEY` | **yes** | | Access key the application uses |
| `MINIO_SECRET_KEY` | **yes** (secret) | | Secret key, at least 8 characters |
| `MINIO_BUCKET` | no | `ccp-raw` | Bucket for raw transcripts; created by the application on first use |
| `MINIO_SECURE` | no | `false` | `true` to use HTTPS |

Docker Compose also reads `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `MINIO_API_PORT` and
`MINIO_CONSOLE_PORT` (see [setup.md](setup.md)). Locally, `MINIO_ACCESS_KEY` and
`MINIO_SECRET_KEY` equal the root user and password. On a server, give the application its own
service account with access to one bucket instead of the root credentials.

### Ingestion API

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `CCP_API_HOST` | no | `127.0.0.1` | Address the API listens on |
| `CCP_API_PORT` | no | `8000` | Port the API listens on (1-65535) |
| `CCP_INGEST_TOKEN` | **yes** (secret) | | Token sent with every upload; at least 16 characters |
| `CCP_SESSION_IDLE_MINUTES` | no | `30` | Minutes without activity before a session is marked closed (1-1440); see [ingestion-api.md](ingestion-api.md#idle-sessions) |
| `CCP_MAX_CHUNK_BYTES` | no | `8388608` | Largest transcript chunk the API accepts, in bytes (1 KiB to 64 MiB) |

### Dashboards (optional)

Used by `ccp-db create-reader`, `ccp-dashboards setup` and the Metabase service of the Compose file; see
[dashboards.md](dashboards.md).

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `CCP_READER_PASSWORD` | **yes** for dashboards (secret) | | Password of the read-only database role (at least 12 characters). Set by `ccp-db create-reader`, used by Metabase |
| `CCP_READER_USER` | no | `ccp_reader` | Name of that role (`ccp-db create-reader` takes `--name` instead) |
| `CCP_AUDITOR_PASSWORD` | only for `ccp-db create-auditor` (secret) | | Password of the auditors' read-only role that can read everything (at least 12 characters); see [access.md](access.md) |
| `CCP_CATALOG_ENABLED`, `CCP_DECISIONS_ENABLED`, `CCP_EXPLORER_ENABLED` | no | `false` | Turn on the readable catalog, the AI-written decisions and the Explorer website inside `ccp-server`; see [install.md](install.md) |
| `CCP_EXPLORER_SECRET` | with the Explorer (secret) | | Signs the Explorer's cookies and forms (at least 16 characters); see [explorer.md](explorer.md) |
| `CCP_SERVER_MIGRATE`, `CCP_SERVER_BIND` | no | `false`, `127.0.0.1` | Apply migrations when `ccp-server` starts; address the container publishes on; see [install.md](install.md) |
| `CCP_DECISION_IDLE_MINUTES`, `CCP_DECISION_SWEEP_MINUTES` | no | `15`, `2` | A chat is given decisions when a session of it ends, or after this many quiet minutes if no end arrives; how often the server looks; see [decisions.md](decisions.md) |
| `CCP_METABASE_ADMIN_EMAIL` | **yes** for dashboards | | Address of the Metabase administrator that `ccp-dashboards setup` creates the first time. Use your own |
| `CCP_METABASE_ADMIN_PASSWORD` | **yes** for dashboards (secret) | | That administrator's password, at least 12 characters |
| `METABASE_PORT` | no | `3000` | Port on this machine where Metabase answers (bound to `127.0.0.1`) |
| `METABASE_URL` | no | `http://127.0.0.1:<METABASE_PORT>` | Address `ccp-dashboards` uses to reach Metabase |
| `CCP_METABASE_DB_HOST` | no | `postgres` | PostgreSQL as Metabase sees it: inside the Compose network it is `postgres` |
| `CCP_METABASE_DB_PORT` | no | `5432` | The port of that PostgreSQL (1-65535) |

### Capture client

Read from **environment variables only**; a `.env` file is deliberately not read (see
[capture-client.md](capture-client.md)). Set them in Claude Code's settings `env` block.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `CCP_API_URL` | no | `http://127.0.0.1:8000` | Base URL of the ingestion API (`http://` or `https://`) |
| `CCP_INGEST_TOKEN` | **yes** (secret) | | The same token the server expects; at least 16 characters |
| `CCP_STATE_DIR` | no | `~/.ccp/capture` | Where the client remembers how far each transcript was uploaded |
| `CCP_CLIENT_CHUNK_BYTES` | no | `1048576` | Target size of an uploaded chunk, in source bytes (1 KiB to 8 MiB) |
| `CCP_CLIENT_TIMEOUT_SECONDS` | no | `15` | Time limit for each HTTP request (1-120) |
| `CCP_CLIENT_BUDGET_SECONDS` | no | `20` | Total time one sync may take before it stops cleanly (1-600) |
| `CCP_SNAPSHOT_REMOTE` | no | | Git URL of the capture project for the repository (see [snapshots.md](snapshots.md)) |
| `CCP_SNAPSHOT_REMOTE_BASE` | no | | Or a group URL: the project is `<base>/<repository name>-capture.git` |
| `CCP_SNAPSHOTS` | no | on if a remote is set, else off | `true` takes snapshots even without a remote (hidden refs stay in the local repository) |
| `CCP_CAPTURE_DISABLED` | no | off | `1` or `true` switches the hooks off completely: no upload, no state, no log, no output (used for CCP's own background Claude runs; see [hooks.md](hooks.md#kill-switch)) |

## Using settings in code

```python
from ccp.settings import load_settings

settings = load_settings()  # reads .env, then the environment; validates everything
settings.database.host  # "127.0.0.1"
settings.database.password  # Secret(***)  - safe to log
settings.database.password.reveal()  # the real value: use only where it is needed
```

If anything is missing or invalid, `load_settings()` raises `SettingsError` listing every
problem at once.
