# Install: nothing to start by hand

CCP has two sides. The **server** runs once and stays on. Each **developer machine** installs the
capture hooks once; after that every Claude Code chat is uploaded by itself.

## Developer machine: 2 commands

```bash
pip install .                                     # in this repository; no other packages needed
ccp-capture install --api-url http://SERVER:8000 --token THE_INGEST_TOKEN
```

`ccp-capture install` merges into `~/.claude/settings.json`:

- the variables `CCP_API_URL`, `CCP_INGEST_TOKEN` and `CCP_SNAPSHOTS=true`;
- the eight hooks (`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`,
  `SessionEnd`), each running `"<full path to this Python>" -m ccp.capture hook`.

It keeps every other setting and every hook of other tools, makes a copy
`settings.json.bak-<time>` first, and can be run again at any time (hooks are never duplicated;
running it again after moving Python or changing the address updates them). If the file is not valid
JSON it stops with an error and changes nothing. `--token` can be left out if `CCP_INGEST_TOKEN` is
set in the shell (so the token stays out of your shell history). Details of the hooks:
[hooks.md](hooks.md).

Then check it, and upload the chats you already have:

```bash
ccp-capture status                 # installed yes/no, API reachable, token valid, pending, last upload
ccp-capture backfill --dry-run     # count what would be uploaded, per project
ccp-capture backfill               # upload every chat in ~/.claude/projects (also --since DAYS)
ccp-capture uninstall              # remove only our hooks and variables
```

`backfill` uses the same upload code as the hooks: everything is redacted on your machine first, and
what is already uploaded is skipped, so running it again is safe (it stops early, and says so, if the
API cannot be reached or refuses the token). It also uploads each chat's sub-agent transcripts and
tool-result files. `backfill` and `status` read the address and token that `install` wrote, so they
work in a plain terminal; variables set in the shell take precedence. `install`, `uninstall`,
`status` and `backfill` take `--settings-path FILE` to work on another settings file.

## Server: 1 command

With Docker (PostgreSQL, MinIO and the server, restarted automatically after a reboot):

```bash
docker compose --env-file .env -f infra/docker-compose.yml up -d
```

The `ccp-server` service reads the same `.env`, applies pending database migrations on start
(`CCP_SERVER_MIGRATE`) and listens on port 8000, on this machine only. To let other machines upload,
set `CCP_SERVER_BIND=0.0.0.0` in `.env` (and put TLS in front of it for anything beyond a trusted
network). Port 8000 is used by `ccp-ingest` if you started it by hand: stop that first.

Without Docker, the same server as a systemd user service (see the comments in
[infra/ccp-server.service](../infra/ccp-server.service)):

```bash
cp infra/ccp-server.service ~/.config/systemd/user/ && systemctl --user enable --now ccp-server
```

or just `python -m ccp.server --migrate` (the same as the `ccp-server` command).

## What the server process does

One process, `ccp.server.main:main`:

1. the **ingestion API** (unchanged, including `/health` and the idle-session closer);
2. the **indexer** in a background thread: a chat is indexed within seconds of its upload, so
   `ccp-index run` is no longer needed;
3. the **plug-ins** that are switched on. Each is off unless its variable is `true`:
   `CCP_CATALOG_ENABLED`, `CCP_DECISIONS_ENABLED`, `CCP_EXPLORER_ENABLED`. A switched-on plug-in
   whose package is not installed is skipped quietly.

On SIGTERM (`docker stop`, `systemctl stop`, Ctrl-C) it stops taking requests, lets the indexer
finish the job it is on and exits.

### Writing a plug-in

A package `ccp.<name>` (one of the three names above) with a function

```python
def register(registry: PluginRegistry, context: PluginContext) -> None:
    registry.add_after_index(callback)        # callback(session_id), after each finished index job
    registry.add_mount("/explorer", asgi_app) # an extra web application next to the API
```

(`ccp.server.plugins`). The context carries the settings, the object store and the environment.
Callbacks run in their own thread, one session at a time, so a slow one never delays indexing; a
callback that raises is logged and does not affect the others. A session is reported again after
each later indexing job, so a callback must be safe to repeat. A plug-in that fails to load or to
register is skipped with a warning; the API keeps running. The paths `/health` and `/v1` are
reserved.
