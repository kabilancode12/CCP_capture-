# Database and migrations

PostgreSQL holds the structured data. The schema is managed by numbered SQL files in
[`db/migrations/`](../db/migrations/) and applied by the `ccp-db` command
([`src/ccp/db/`](../src/ccp/db/)).

## Commands

Start PostgreSQL first (see [setup.md](setup.md)). The connection comes from the `POSTGRES_*`
variables in `.env` (see [configuration.md](configuration.md)).

| Task | Command |
| --- | --- |
| Show which migrations are applied | `ccp-db status` |
| Apply everything pending | `ccp-db migrate` |
| Apply up to a version | `ccp-db migrate --target 1` |
| Undo the latest migration | `ccp-db rollback` |
| Undo the latest two | `ccp-db rollback --steps 2` |

If the `ccp-db` launcher is blocked on your machine, use `python -m ccp.db <command>`.
Rollback **deletes data** (it drops the tables); use it on a development database only.

## Writing a migration

1. Add the next numbered file: `db/migrations/002_<short_name>.sql` (lower-case, digits and
   underscores only). Versions start at `001` and have no gaps.
2. Add `002_<short_name>.down.sql` that undoes it. Without it, rollback is refused.
3. Never edit a migration that has been applied anywhere. The runner stores a checksum of each
   applied file and refuses to continue if one has changed. To change the schema, add a new
   migration.
4. Do not write a literal `%` in the SQL; the driver treats it as a parameter marker. Write
   `%%`.
5. Test it: `pytest tests/integration` applies every migration to a throwaway database, rolls
   it back and applies it again.

## How the runner behaves

| Behaviour | Why |
| --- | --- |
| Each migration runs in its own transaction | A failing migration leaves no partial change; earlier ones stay applied |
| Applied files are checked by checksum | Detects an edited migration before it causes drift between databases |
| A database lock is held while migrating | Two runners cannot migrate at the same time |
| Applied versions are stored in `schema_migration` | The database knows its own version |
| Line endings are ignored in the checksum | Windows and Linux checkouts agree |

## Tables (migrations 001 to 003)

| Table | Holds |
| --- | --- |
| `session` | One row per Claude Code session |
| `session_env` | Who ran it and where: version, surface, shell, OS, account |
| `event` | Messages, tool calls and decisions, with a pointer to the raw transcript line (filled in Step 2) |
| `redaction_log` | Rule, location and count of each redaction; never the redacted value (unique per session, rule and location, so a repeated report is safe) |
| `job` | Work queue for indexing and distillation (at most one queued index job per session) |

The full planned design (all steps) is in [`db/drafts/001_init.sql`](../db/drafts/001_init.sql).

## Driver

The application uses **pg8000**, a pure-Python PostgreSQL driver. The more common `psycopg`
needs a compiled library, which company Application Control policy blocks on some machines.
Connect with `127.0.0.1`, not `localhost`: on Windows, `localhost` tries IPv6 first and adds
about 2 seconds to every connection.

## Tests

`tests/integration/` needs the local PostgreSQL. Each test creates a throwaway database
(`ccp_test_<random>`) and drops it afterwards, so the real `ccp` database is never touched. If
PostgreSQL is not running, these tests are skipped, not failed.
