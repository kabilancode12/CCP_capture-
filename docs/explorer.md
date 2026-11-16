# CCP Explorer

A website for browsing captured chats: Claude account, then project, then chat, with its
conversation, sessions, decisions, tests, code changes, corrections, sub-agents, thinking notes and
usage. It reads the index in PostgreSQL (migration 016) and nothing else.

It is server-rendered with Starlette and the standard library only: no template engine, no
JavaScript, no extra packages. Passwords are hashed with scrypt, the session is a signed cookie
(HMAC-SHA256), and every form that changes something carries a CSRF token.

## Start it

```powershell
# once: a secret for the cookies (16+ characters) in .env
#   CCP_EXPLORER_SECRET=<long random text>
python -m ccp.explorer create-user admin@example.com --role admin
python -m ccp.explorer serve --port 8090
```

`create-user` asks for the password twice, or reads `CCP_EXPLORER_PASSWORD` (12 or more
characters). `serve` is for testing on its own; the site is built to be mounted at `/explorer` in the
server:

```python
from ccp.explorer.app import create_app

app = create_app(database_settings, store=None, secret=secret)   # Mount("/explorer", app=app)
```

Every link and the cookie path follow the mount path, so any prefix works.

## Roles

| Role | Chats | Also |
| --- | --- | --- |
| `developer` | Full content of the chats of their own Claude account (`ui_user.account_id`). Another account's chat gives 404; in the project list it shows counts only, with a neutral account name | - |
| `lead` | Every chat | Approve or reject decisions |
| `auditor` | Every chat | Approve or reject decisions; read the access log |
| `admin` | Every chat | Review decisions; manage users and labels; read the access log |

Opening a chat (any tab) writes a row to `ui_access_log` with the action `view_chat` and the chat id.
Logging in and out and reviewing a decision are logged too. An admin cannot disable or demote their
own user, so the site cannot be left without an admin.

## Pages

| Address | What |
| --- | --- |
| `/login`, `/logout` | Sign in and out. Five wrong passwords for one address block it for ten minutes |
| `/` | Tree of Claude accounts and projects: chats, empty chats, tokens, last activity |
| `/project/<id>` | Chats of a project: date, title, sessions, prompts, tokens, tests passed/failed, decisions, corrections. Empty chats are hidden; a link toggles them |
| `/chat/<id>?tab=...` | Tabs: `conversation`, `sessions`, `decisions`, `tests`, `code`, `corrections`, `agents`, `thinking`, `usage` |
| `/admin/users` | Create users; change role, linked Claude account, disabled state; reset password |
| `/admin/labels` | Labels of accounts and projects (the names dashboards show), through `ccp.db.labels` |
| `/access-log` | Who opened what, newest first (admin and auditor) |

The conversation tab shows prompts and replies from the main transcript; tool calls are one line each
(`Bash: pytest -q`) and tool results only say whether they worked, failed or were refused, so large
or sensitive output is never put on a page. Thinking notes are summarised reasoning, context only.

## Reviewing decisions

The decisions tab shows each decision with a badge and who reviewed it: "approved by AI
(claude-opus-5-5)" or "rejected by Ana". Leads, auditors and admins can approve or reject with a note.
The review calls `set_decision_status(connection, decision_id, status, reviewer, note)` of
`ccp.decisions` when that package exists (imported only at that moment). Otherwise the page makes the
same change itself: status `approved` or `rejected`, `reviewed_by_kind` `human`, the reviewer's name and
note, and one more entry in `review_history`.

## Security notes

- The cookie is `HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS, and lasts 8 hours. A new session id is
  made at each login. A session ends when the user is disabled or their password changes.
- Pages are sent with a strict `Content-Security-Policy` (no script, styles only from the site),
  `Cache-Control: no-store`, `nosniff` and no referrer.
- Every database value is escaped with `html.escape` before it reaches a page.
- Run it behind HTTPS (a reverse proxy) outside a test machine: passwords travel in forms.

## Settings

| Variable | Meaning |
| --- | --- |
| `CCP_EXPLORER_SECRET` | Signs cookies and form tokens; at least 16 characters; changing it logs everyone out |
| `CCP_EXPLORER_PASSWORD` | Password for `create-user` when not asked interactively |

The database settings are the usual `POSTGRES_*` ones (see [configuration](configuration.md)).


## Approaches, activity and the capture chain

| Page | What it shows |
| --- | --- |
| `/approaches` | search the approach library; open one for its story, or compare two ([approaches.md](approaches.md)); thinking notes only for leads, auditors and admins |
| `/timeline` | the decisions in time order, with what replaced what |
| `/review` | decisions the AI reviewer sent to a person |
| `/activity` | per developer, machine and branch: last change, changes by a person and by Claude, files touched |
| `/insights` | changes by actor and model, decisions by who decided, approaches by state, most reused |
| `/alerts` | newer work whose title resembles a removed approach; reviews waiting |
| chat tab **Changes by cause** | every change made during the chat with who made it (person, edit tool, shell command, sub-agent) |
| chat page link | **Download the whole chat as one .md file** ([catalog.md](catalog.md)) |

All of them need a signed-in user; the chat tabs keep their usual per-account access rule.
