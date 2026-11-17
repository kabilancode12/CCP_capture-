# Who can see what (access roles)

Everybody may see **how much** Claude Code is used. Only a few people may see **what was said**. The database
enforces this with two read-only roles; neither can change anything.

| Who | Connects as | Sees | Never sees |
| --- | --- | --- | --- |
| Developers and team leads | Metabase dashboards, which read as `ccp_reader` | Counts, tokens, costs, models, projects and Claude accounts by their labels (the `d_` views, see [dashboards.md](dashboards.md)) | Prompts, replies, file contents, e-mail addresses, names of people and machines, full folder paths |
| Auditors (a few named people) | `ccp_auditor`, with a SQL tool such as DBeaver | Everything, read-only: the chat history (`event`), the environment record (`session_env`), the audit views of [queries.md](queries.md) | Nothing is hidden; nothing can be changed |
| Administrator | `ccp` (the owner, from `.env`) | Everything; runs migrations, makes roles, sets labels | |

## Set it up

```bash
ccp-db create-reader      # role for the dashboards; password CCP_READER_PASSWORD
ccp-db create-auditor     # role for auditors;       password CCP_AUDITOR_PASSWORD
```

Both commands can be run again at any time: they update the password and the grants. **Run `ccp-db
create-auditor` again after `ccp-db migrate`** when a migration added tables, so the auditor can read them.

Inside Metabase, every person gets their own Metabase login (Admin settings, People); all of them see the
dashboards, and only the Metabase administrator can change them. Metabase reads the database as `ccp_reader`,
so even a Metabase user who writes their own SQL question can reach only the `d_` views.

## Names on the dashboards: labels

The usage cards show a project by its name (the last part of its Git remote or folder, `ccp_V2`; a home folder
as a neutral `project-3f9a1c`, since its last part is a login name) and a Claude account as a neutral name
(`account-3f9a1c`). Give them real names:

```bash
ccp-db labels                                            # every account and project, with its label
ccp-db label account someone@example.com claude1         # the account is shown as "claude1"
ccp-db label account 6f1c2d3e-...-00000000abcd claude1   # or by its account uuid
ccp-db label project billing billing-api                 # the project named "billing"
ccp-db label project /home/dev/work/billing billing-api  # or by one of its folders, or its key
ccp-db unlabel account someone@example.com               # back to the neutral name
```

Labels are stored on the account and project rows (`claude_account.label`, `project.label`, migration 016). A
project is chosen by its key or Git remote, by one of its folders, or by its name when only one project has that
name. An account is chosen by its e-mail address or account uuid; one that matches no account yet is added
(anything else is refused). A project with a Git remote is one project wherever it is cloned; a folder without a remote is a project of
its own, and two such projects may share a label to be counted as one.

`ccp-db labels` prints e-mail addresses and folder paths: it is for the administrator. A label is 1 to 40
letters, digits, spaces, dots, dashes or underscores.

The dashboard reader cannot read `claude_account`, `project` or the view `chat` (they hold e-mail addresses and
folders); it reads `d_chat` and `d_usage`, which show only labels or neutral names.

## Limits

- **Per-developer numbers are not on the dashboards.** Showing usage per person is a decision for the team
  (see Step 5 in [TODO.md](TODO.md)); until then it is available to auditors only, through `session_env`.
- **One role per tier, not per team.** Metabase's free edition cannot limit rows per user, so a team lead sees
  every team's usage. Separate per-team views are possible later.
- The connection is plain and local for now; HTTPS and sign-in for a shared server come with Step 5.
