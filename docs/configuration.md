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
| `MINIO_BUCKET` | no | `ccp-raw` | Bucket for raw transcripts |
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
| `CCP_SESSION_IDLE_MINUTES` | no | `30` | Minutes without activity before a session is marked closed (1-1440) |

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
