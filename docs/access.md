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

The usage cards show a project as the last part of its folder (`ccp_V2`) and a Claude account as a neutral name
(`account-3f9a1c`). Give them real names:

```bash
ccp-db labels                                            # every account and folder seen, with its label
ccp-db label account someone@example.com claude1         # the account is shown as "claude1"
ccp-db label project /home/dev/work/billing billing-api  # that folder is shown as "billing-api"
ccp-db unlabel account someone@example.com               # back to the neutral name
```

`ccp-db labels` prints e-mail addresses and folder paths: it is for the administrator. A label is 1 to 40
letters, digits, spaces, dots, dashes or underscores. Two folders may share a label, so a project checked out
in different places (or on different machines) can be counted as one.

## Limits

- **Per-developer numbers are not on the dashboards.** Showing usage per person is a decision for the team
  (see Step 5 in [TODO.md](TODO.md)); until then it is available to auditors only, through `session_env`.
- **One role per tier, not per team.** Metabase's free edition cannot limit rows per user, so a team lead sees
  every team's usage. Separate per-team views are possible later.
- The connection is plain and local for now; HTTPS and sign-in for a shared server come with Step 5.
