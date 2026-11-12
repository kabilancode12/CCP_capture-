# Hooks: connecting Claude Code to the capture client

Claude Code runs a command for each *hook event* and sends it a JSON payload on standard input.
The capture client registers for six events and, for each, uploads what is new. The code is in
[`src/ccp/capture/hooks.py`](../src/ccp/capture/hooks.py) (what to do) and
[`hook_runner.py`](../src/ccp/capture/hook_runner.py) (running it safely).

> **Private mode is not implemented yet.** It is on hold by decision. When it is added, it will be a
> switch the hook handler checks before uploading anything.

## What each event does

| Event | Action | Time limit given to Claude Code |
| --- | --- | --- |
| `SessionStart` | Record the environment; catch up on earlier sessions; sync this session if its transcript exists (resume, compact); baseline code snapshot and branch backup | 30 s (uploads use at most 8 s) |
| `UserPromptSubmit` | Sync the session. Claude waits for this hook, so it gets the shortest upload budget (5 s) | 15 s |
| `Stop` | Sync the session; complete the environment record; then take a [code snapshot](snapshots.md) if the working tree changed | 30 s (uploads use at most 15 s) |
| `SubagentStop` | Sync the session, **except** for Claude's internal helpers (empty `agent_type`), which fire after almost every turn and have no transcript | 30 s |
| `PreCompact` | Sync the session before it is compacted | 30 s |
| `SessionEnd` | **Start a detached background flush and return at once**: it uploads what is left and takes the final code snapshot. Best effort only | 5 s |

Why these choices come from what we observed (see [verify/transcript-facts.md](verify/transcript-facts.md)):

- The transcript file **does not exist yet** at the first `SessionStart` and `UserPromptSubmit`, so
  those events only prepare; the first real upload is at `Stop`.
- `Stop` does **not** fire after a turn ended by a permission denial, and fires while an asynchronous
  sub-agent is still running. So several events sync, and every sync picks up whatever an earlier
  one missed.
- `SessionStart` runs **again** after `/compact` and on resume. Everything is idempotent, and the
  environment record is collected only until it is complete.
- `SessionEnd` fires on a normal exit, but Claude Code gives it very little time and may not wait.
  Nothing depends on it: the background flush is a bonus, and the **catch-up at the next
  SessionStart** is the safety net.

## The rules a hook must obey

| Rule | Why | How |
| --- | --- | --- |
| **Always exit 0** | Exit code 2 can stop Claude from finishing | Every error is caught and logged |
| **Print nothing** | A SessionStart hook's output is added to the conversation | Output goes to the log file only; tests check stdout and stderr are empty |
| **Never block Claude for long** | Hooks that Claude waits for add latency | Short upload budgets; after the first "API unavailable" nothing else is tried |
| **Log no content** | Logs are not redacted | Event names, ids, counts and short reasons only; no prompts, paths, values or tokens |
| **Be safe to repeat** | Events repeat and some are skipped | Uploads are idempotent; positions move only after success |

## Setting it up on a developer machine (Windows)

1. Install the client from this repository with **no extra packages**:
   `pip install .` (the server's packages are in the `server` extra and are not needed here).
   Check: `python -m ccp.capture hook-settings` prints JSON.
2. Print the settings: `python -m ccp.capture hook-settings --api-url http://<server>:8000`.
3. Merge the `env` and `hooks` blocks into `~/.claude/settings.json` (all projects) or a project's
   `.claude/settings.json`. Replace `REPLACE_WITH_THE_INGEST_TOKEN` with the real token, by hand: the
   generator never copies a token from your environment.
4. Start Claude Code in a repository and run `/hooks`: six capture hooks should be listed.
5. Watch the log: `Get-Content "$HOME\.ccp\capture\hook.log" -Wait -Tail 20`. After a prompt you should
   see lines such as `Stop session=... 1 chunk(s), 3 attachment(s) uploaded ... (0.7s)`.

The command is written as `"<python>" -m ccp.capture hook`, with the full path to Python in forward
slashes. This is deliberate: on Windows, hooks run in Git Bash, and plain `bash` or `python` may
resolve to the wrong program (WSL, or the Microsoft Store stub).

## The log

`<state folder>/hook.log` (default `~/.ccp/capture/hook.log`, or `CCP_STATE_DIR`), rotated at 1 MB with
three old files kept. One line per hook:

```text
Stop session=4a64fad3-... 1 chunk(s), 0 attachment(s) uploaded; environment: sent (0.7s)
SubagentStop session=4a64fad3-... skipped: internal helper (no agent type) (0.0s)
SessionEnd session=4a64fad3-... flush started in the background (0.0s)
```

If the settings are invalid (for example no token), the hook still exits 0 and logs
`settings invalid: ... CCP_INGEST_TOKEN is required` (the variable name, never a value).

## What was checked

- Unit tests: payload parsing, every event's action and time limit, "never fails, prints nothing, logs
  no content", the settings generator (including that a real token is never copied), and the
  detached process.
- Integration tests run each hook as a **separate process**, as Claude Code does, against the real API,
  PostgreSQL and MinIO: a scripted session through all six events, the API going down and coming back,
  and catch-up at the start of a new session.
- A live run of real Claude Code with the generated settings captured the session, including the
  second chunk uploaded by the background flush after SessionEnd.

## Limits

- **The SessionEnd flush is best effort.** It is started outside Claude Code's job object when Windows
  allows it, but the operating system or a company policy may still end it. The next SessionStart
  uploads whatever is left.
- A machine that never starts Claude Code again leaves that last bit unsent.
- Other surfaces (VS Code extension, JetBrains, Desktop app) have not been tested with these hooks.
