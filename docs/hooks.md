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
| `SessionStart` | Queue the session start (see below); record the environment; catch up on earlier sessions (this also sends the start); sync this session if its transcript exists (resume, compact); baseline code snapshot and branch backup | 30 s (uploads use at most 8 s) |
| `UserPromptSubmit` | Sync the session, but not its tool-result files (one large file could take longer than the budget, which is checked only between files; `Stop` uploads them). Claude waits for this hook, so it gets the shortest upload budget (5 s) | 15 s |
| `Stop` | Sync the session; complete the environment record; then take a [code snapshot](snapshots.md) if the working tree changed | 30 s (uploads use at most 15 s) |
| `SubagentStop` | Sync the session, **except** for Claude's internal helpers (empty `agent_type`), which fire after almost every turn and have no transcript | 30 s |
| `PreCompact` | Sync the session before it is compacted | 30 s |
| `SessionEnd` | Queue the session end (see below), then **start a detached background flush and return at once**: it sends the end, uploads what is left and takes the final code snapshot. Best effort only. A chat without a transcript (opened and closed without a message) still gets its end sent | 5 s |

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

## Sessions of a chat: start and end

A chat (one conversation, one transcript) can be opened several times: started, resumed, continued after
`/compact`. Each opening is a **session** (table `chat_session`, see [database.md](database.md#the-chat-hierarchy-migration-016)).

- `SessionStart` makes a new **run key** (a UUID), remembers it in the state folder for this chat, and queues
  `{"kind": "start", "run_key", "at", "source", "commit"}` for `POST /v1/sessions/{id}/lifecycle`. `source` is
  the payload's `startup | resume | clear | compact`; `commit` is the Git HEAD of the working folder (read with
  a 2-second limit; `null` outside a repository). The catch-up that follows sends it with the waiting events
  of other chats; it is not sent on its own, so that an unreachable API costs the hook one timeout only.
  The hooks' API client also **fails fast**: once one request could not reach the API (no connection or a
  timeout, for example off VPN), every later request of that hook run fails at once instead of waiting for
  its own timeout. A server that answers, even with an error, does not trigger this.
- `SessionEnd` builds the end with the same run key and the payload's `reason`
  (`clear | resume | logout | prompt_input_exit | other`, verified on Claude Code 2.1.325). It does not wait for
  Git (Claude Code may stop the hook at any moment): it writes the end to the state folder and starts the
  background flush at once. The flush reads HEAD, adds it to the end (`commit`) and sends it. If the flush
  never runs, the next catch-up sends the end without a commit. Known gap (rare, accepted): if a catch-up
  started by another chat sends the end before the flush added the commit, that session's `end_commit` stays
  empty. An end without a known run key gets a fresh
  key. For a chat without a transcript (opened and closed without a message) the flush only sends the end: no
  upload and no final code snapshot.
- Events wait in the state folder until the API has them (`lifecycle-<kind>-<run key>.marker`); every sync and
  every catch-up sends what waits. Repeating one is harmless: the server keys rows by run key.
- `/compact` runs no `SessionEnd`; the server ends the earlier open session when the next one starts
  (`end_reason = inferred`). A session that never gets an end is ended by the idle closer (`idle_timeout`).
- An older server answers 404: the client logs it once, keeps the events, and asks again an hour later.
- On the server, a start that arrives late (it waited on the machine) and is older than the chat's last
  activity or recorded end does not reopen the chat. A newer start reopens it and clears its end until the
  next end (or the idle closer) sets it again. Start times are the developer machine's clock.

## Kill switch

If `CCP_CAPTURE_DISABLED` is `1` or `true`, the hook does nothing at all: no upload, no state, no log line, no
output, exit code 0. CCP sets it for its own background Claude runs, so that they are not captured.

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
SessionEnd session=4a64fad3-... end queued; flush started in the background (0.0s)
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
