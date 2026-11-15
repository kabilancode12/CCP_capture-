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
| Make the read-only role for dashboards | `ccp-db create-reader` (see [dashboards.md](dashboards.md)) |
| Make the read-only role for auditors | `ccp-db create-auditor` (see [access.md](access.md)) |
| Name Claude accounts and projects on the dashboards | `ccp-db labels`, `ccp-db label ...`, `ccp-db unlabel ...` (see [access.md](access.md)) |

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

## The chat hierarchy (migration 016)

Every captured conversation is organised as **Claude account → project → chat → its parts**:

| Name | Means | Table |
| --- | --- | --- |
| Claude account | One person's Claude login | `claude_account` |
| Project | One Git repository (its `origin` remote, normalised: `github.com/acme/billing`), wherever it is cloned; a folder without a remote is its own project (`<host>:<folder>`) | `project` |
| Chat | One Claude Code conversation: one id, one transcript. `/clear` starts a new chat | `session` (kept for compatibility); the view `chat` shows it with friendly names |
| Session | Each time a chat is opened: start, resume, after `/clear` or `/compact`, until SessionEnd or the idle timeout | `chat_session` |
| Decision | One task's goal, options, choice and reason, inside a chat; reviewed by a person or an AI (`reviewed_by_kind`) | `decision` |
| Test run / test | One test command, and each test in it | `test_run`, `test_case` |
| Correction | Something put right: a failing test fixed, a retried command, rework, a rewind, a refusal, the developer correcting Claude | `correction` |

A chat is linked to its account and project when its environment record arrives (`ccp.db.hierarchy`); the
view `chat` also finds them for a chat that is not linked yet (by account uuid or e-mail, and by folder).
Chats opened and closed without a message are marked `is_empty` and left out of `d_chat`.

A project can be labelled before any chat ran in it (`ccp-db label project <folder>`): it is then keyed by the
folder alone. A folder without a Git remote is its own project, keyed `<host>:<folder>`. Either such *folder
project* is merged into the chat's project as soon as a chat in that folder has another key (a Git remote, or
`<host>:<folder>` for a folder labelled in advance): the chat's project takes its label if it has none of its
own, and its chats and folders; the folder project is deleted. So one repository is one project, and a label
is kept when a folder gets a Git remote. Windows folders are keyed with a lower-case drive letter and backslashes
(`c:\work\app`), since Claude Code writes the drive letter in either case.

`decision` is **not** index data: a rebuild of the index never deletes it. Its evidence is kept as raw
transcript lines (`evidence_lines`: `[{"key": "<raw object key>", "line": n}]`), not as event ids, because a
rebuild numbers the events anew. `ui_user` and `ui_access_log` hold
the users of the explorer website and what they opened. The new job types `distil_chat` and `catalog_chat`
need no schema change (`job.job_type` has no CHECK).

## Tables (migrations 001 to 016)

| Table | Holds |
| --- | --- |
| `session` | One row per chat (Claude Code conversation), with its account, project, title and `is_empty` (016) |
| `session_env` | Who ran it and where: version, surface, shell, OS, account (uuid, role), a hash of the machine id, folder, Git remote and Git identity |
| `chat_session` | Each opening of a chat: how it started, when and why it ended, the commits at start and end (016) |
| `claude_account`, `project` | The Claude accounts and projects, with the labels shown on dashboards (016) |
| `event` | One row per transcript line, with a pointer to the raw line ([indexer.md](indexer.md)) |
| `compaction` | One row per compaction: trigger, tokens before and after, summary text |
| `model_usage` | Tokens per model response (de-duplicated by message id), with effort and thinking time (016) |
| `session_cost`, `session_model_cost` | Session cost and tokens per model, read from the latest `cost-state` entry |
| `agent_run` | One row per sub-agent run, linked to the call that launched it |
| `file_change` | One row per AI edit of a file: path, changed line ranges, the call and prompt behind it |
| `test_run` | One row per test run: runner, command, passed or failed, exit code, tests passed, failed and skipped (016) |
| `test_case`, `correction`, `decision` | Each test of a run; each correction; each decision (016, filled by later steps) |
| `ui_user`, `ui_access_log` | Explorer website users and their access log (016) |
| `commit_link` | Which session produced a commit, with a confidence ([audit.md](audit.md#commit-linking)) |
| `thinking` | The model's reasoning blocks of a message (text is empty on Claude Code 2.1.289); restricted |
| `redaction_log` | Rule, location and count of each redaction; never the redacted value (unique per session, rule and location, so a repeated report is safe) |
| `job` | Work queue for indexing and distillation (at most one queued index job per session) |

**After applying 016, run `ccp-db create-reader` and `ccp-db create-auditor` again.** The migration drops and
recreates `d_usage`; it carries over the right to read it and gives the same roles the new `d_chat`, but only
the commands bring the roles exactly up to date (the auditor also needs the new tables and views).

Migration 016 replaces the label tables of 015 (`account_label`, `project_label`) by `claude_account.label` and
`project.label`, and adds the views `chat`, `d_chat` (dashboard: one row per chat with messages, counts only),
`v_slash_command` (name and arguments of each slash command) and `v_workflow_run` (one row per Workflow run,
with its sub-agents and the call that launched it). Migration 015 adds the dashboard view `d_usage` (tokens and cost per session and model, with project and account
labels) and the helper view `v_session_folder`. Migration 013 adds five views for dashboards (`d_session`, `d_model_cost`, `d_edit`, `d_test_run`, `d_fix_loop`; counts
and categories only, see [dashboards.md](dashboards.md)). Migration 011 adds six read-only views (`v_session_cost`, `v_file_history`, `v_agent_run`,
`v_human_decision`, `v_compaction`, `v_test_fix_cycle`) that answer the audit questions: see
[queries.md](queries.md).

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
