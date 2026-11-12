# The environment record

One record per session (the `session_env` table) says **who ran it, with which Claude Code, and
where**. The type and its validation are in [`src/ccp/environment.py`](../src/ccp/environment.py);
collecting it is in [`src/ccp/capture/environment.py`](../src/ccp/capture/environment.py).

> **This record is personal data.** It holds the user name, host name and the account's name and
> e-mail address. It is collected on purpose (the plan asks for it) and, per the plan, it is **kept
> in private mode too**: private mode keeps a minimal record of session, developer, account and
> environment. Restrict access to the `session_env` table (task for Step 5), and tell developers
> what is collected before the pilot.

## Fields and where each comes from

| Field | Example | Source | Stage |
| --- | --- | --- | --- |
| `entrypoint` | `cli`, `claude-vscode` | `CLAUDE_CODE_ENTRYPOINT` (only set in processes Claude Code starts) | start |
| `surface` | `terminal`, `vscode` | derived from the entrypoint, then other markers `[verify]` | start |
| `hook_shell` | `git-bash` | the shell the hook runs in (`MSYSTEM`, `SHELL`, ...) `[verify]` | start |
| `access_path` | `local`, `ssh`, `container` | `SSH_CONNECTION`, container markers `[verify]` | start |
| `os_name`, `os_version`, `cpu_architecture` | `Windows`, `11 10.0.26200`, `AMD64` | the operating system | start |
| `os_user`, `hostname` | | the operating system | start |
| `account_name`, `account_email`, `organisation_name`, `organisation_uuid` | | `~/.claude.json`, key `oauthAccount` | start |
| `auth_type` | `subscription`, `api_key` | `ANTHROPIC_API_KEY` set means `api_key` (only whether it is set, never the key); otherwise a login means `subscription` | start |
| `claude_code_version` | `2.1.289` | the `version` key of transcript entries | transcript |
| `tool_shell` | `PowerShell (primary); Bash tool also available ...` | the transcript's `environment` entry (prose, as Claude wrote it) | transcript |
| `os_version` (more precise) | `Windows 11 Home Single Language 10.0.26200` | the same `environment` entry; replaces the start value | transcript |
| `run_mode` | | **not collected yet**: no reliable way to tell interactive, headless and SDK runs apart has been established | |

## Two stages

The transcript file does not exist yet at SessionStart (see
[verify/transcript-facts.md](verify/transcript-facts.md), fact 7), so the version and the tool shell
cannot be read then.

1. **At SessionStart**, inside the hook: `collect_process_environment` gathers what the process,
   the operating system and the login file say. It must run inside a hook: running it by hand in your
   own terminal cannot see `CLAUDE_CODE_ENTRYPOINT`.
2. **Once the transcript exists** (the first Stop): `collect_transcript_environment` reads only the
   `version` key and the `environment` entry from the first lines. No prompt or answer is read.

```powershell
ccp-capture environment --session-id <id>                       # start: process facts
ccp-capture environment --session-id <id> <transcript.jsonl>    # later: adds version and tool shell
```

## How it is sent and merged

The client sends the known fields as `PUT /v1/sessions/{id}/environment`
([ingestion-api.md](ingestion-api.md)). The server merges every update into the stored record: a
field that is present replaces the stored value, a field that is missing leaves it alone. So the two
stages, and any later correction, combine into one record, and repeating an update changes nothing.

The client remembers a hash of what it last sent successfully and sends again only when the record
changes, so calling it at every hook costs nothing once the record is complete.

## Validation on the server

Only the known field names are accepted; every value must be a string of 1 to 512 characters
without control characters; an empty or unknown payload is refused. Error messages never repeat a
value.

## Limits

- `surface`, `hook_shell` and `access_path` are guesses from environment variables, marked
  `[verify]` until each surface (VS Code, JetBrains, Desktop, SSH, web) has been tested.
- `tool_shell` is a sentence, not a clean name. Parse it loosely.
- The shell the **developer** types in (cmd versus PowerShell) cannot be told apart from
  environment variables; the transcript's description of Claude's tool shell is the best source.
