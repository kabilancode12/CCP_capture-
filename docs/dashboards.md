# Dashboards

Usage, cost, models, edits and test fix loops, on the captured data. The dashboards are made in
[Metabase](https://www.metabase.com/) (free, open source), which runs from the Compose file. They are set up by
**code**, not by clicking: the same cards and layout every time, on a new machine or after the data was wiped.
The code is [`src/ccp/dashboards/`](../src/ccp/dashboards/).

## What a dashboard may show, and what it never can

A dashboard shows counts, costs and trends. It must not show what was said. That is enforced by the database,
not by good intentions:

- Metabase connects as a **read-only role** (`ccp_reader`) that can read only five **dashboard views**
  (migration 013): `d_session`, `d_model_cost`, `d_edit`, `d_test_run`, `d_fix_loop`. It cannot read the event
  table (raw transcript lines), the environment record (names of people and machines), the audit views of
  [queries.md](queries.md) (prompts and replies), or anything else, and it cannot change anything.
- The views hold numbers and short categorical text only (surface, model, tool, file path, branch). A test
  fails if a view ever exposes free text or a JSON column.
- Developer identity is **not** in the views. A per-developer breakdown is a decision to be made with the team,
  not a default (see Step 5 in [TODO.md](TODO.md)).

## Set it up

You need the stack and the migrations ([quickstart.md](quickstart.md), parts A2 to A4), and data to look at (a
captured session, indexed with `ccp-index run --once`).

1. **Add the dashboard settings to `.env`** (the block at the end of `.env.example`; every variable is in
   [configuration.md](configuration.md#dashboards-optional)): a password of at least 12 characters for the
   read-only role, and an address and password for the Metabase administrator.

2. **Make the read-only role** (rerun any time; it also brings the grants up to date):

   ```powershell
   ccp-db create-reader
   ```

3. **Start Metabase** (it is not part of the normal stack; the first start downloads the image and takes about a
   minute):

   ```powershell
   docker compose --env-file .env -f infra/docker-compose.yml --profile dashboards up -d metabase
   ```

4. **Make the dashboard:**

   ```powershell
   ccp-dashboards setup
   ```

   The first time it also creates the administrator. It ends with the address, for example
   `Dashboard ready: http://127.0.0.1:3000/dashboard/2`. Open it and sign in with the administrator's address and
   password.

If a launcher is blocked on your machine, use `python -m ccp.db create-reader` and
`python -m ccp.dashboards setup`.

`ccp-dashboards setup` can be run again at any time: it finds what exists by name and changes only what differs,
so it is also how a change to a card or to the layout reaches Metabase. A card or dashboard that someone renames
in Metabase is treated as new, and the old one is left alone.

## What is on it

25 cards in five rows. The questions are in [`definitions.py`](../src/ccp/dashboards/definitions.py), one SQL line
each.

| Row | Cards |
| --- | --- |
| Headline numbers | Sessions, total cost, lines added by AI edits, test pass rate |
| Over time | Sessions per day, cost per day, cost by model, sessions by surface, Claude Code versions |
| The work done | Lines changed per day by actor, edits by tool, edits the developer changed before accepting |
| Fix loops and decisions | Test runs per day, fix loops by test runner, rewinds / refused tool calls / compactions per day, most expensive sessions |
| Tokens by project, model and account | Total tokens, projects, Claude accounts, models; tokens by project, by model and by account; tokens per day by project; a table of usage by project, account and model |

The token cards read `d_usage` (migrations 015, 016). A project is shown by the name of its Git repository, or
else by the last part of its folder (for example `ccp_V2`); a home folder (`/home/<user>`, `/Users/<user>`,
`C:\Users\<user>`, `/root`) is shown as a neutral `project-3f9a1c` instead, because its last part is the
person's login name. An account is a neutral name such as `account-3f9a1c`. An administrator can give both a real
name with `ccp-db label` (see [access.md](access.md)); the dashboard never shows an e-mail address or a full path.
Tokens are Claude Code's own `cost-state` figures when the session has them; otherwise the recorded responses are
summed and the cost stays empty (`token_source` says which).

"Split by actor class" is there (lines changed per day, by actor), but until the code snapshots are used (task
3.4) every change is an `ai_edit`, so the split is not yet informative.

## Add or change a card

1. Edit [`definitions.py`](../src/ccp/dashboards/definitions.py): a `Card` has a name, a SQL question, a display
   type and a place on the 24-column grid. Read only the `d_` views; a test refuses anything else, and the
   layout test refuses overlaps.
2. Run `ccp-dashboards setup`.

A new column or view for dashboards goes in a **new migration** that creates the view; then add it to
`DASHBOARD_VIEWS` in [`db/readers.py`](../src/ccp/db/readers.py) and run `ccp-db create-reader` so that the role
may read it.

**After migration 016, run `ccp-db create-reader` again.** It recreates `d_usage` and adds `d_chat`. The
migration passes the right to read `d_usage` on to the new view and grants `d_chat` to the same roles, so the
usage dashboard keeps working; running the command makes sure the role matches `DASHBOARD_VIEWS` exactly.

## Notes and limits

- Metabase keeps its own settings (users, cards) in a small file database on the `metabase-data` volume.
  `docker compose ... down -v` deletes it together with the data; `ccp-dashboards setup` makes it again. For a
  server, point Metabase at its own PostgreSQL database instead (see Metabase's documentation).
- The image is pinned by tag in the Compose file. Update it deliberately, then rerun `ccp-dashboards setup`; the
  setup uses Metabase's API of version 0.59.
- The views count only kept work (rewound work is left out of lines, edits and prompts). They run on the live
  tables, which is fine for a team's pilot; very large data will need materialised views.
- Local use only: the port is bound to `127.0.0.1`. Putting Metabase on a server (HTTPS, single sign-on, who may
  see what) belongs to Step 5.

## How it is tested

[`tests/integration/test_dashboard.py`](../tests/integration/test_dashboard.py) checks every view against the
invented session of `story.py`, that no view exposes free text, and that the read-only role can read the views and
nothing else (including that it cannot change or create anything).
[`tests/unit/dashboards/`](../tests/unit/dashboards/) checks the definitions (read-only SQL on the dashboard views
only, a layout without overlaps) and the setup steps against a fake Metabase. The whole path was also run against
a real Metabase 0.59.6: all 16 cards answered, with the expected numbers on the invented session, and a second run
changed nothing (still one dashboard, 16 cards).
