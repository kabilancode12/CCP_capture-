# Code snapshots

Claude's conversation is only half the story: the code lives in the developer's working tree, often as
changes that were never committed. After each response the client records the working tree as a
**hidden Git commit**, without the developer doing anything and without touching their repository. The
code is in [`src/ccp/capture/snapshots/`](../src/ccp/capture/snapshots/); this is the Python port of the
first project's `capture.sh`.

## Turning it on

Snapshots write hidden refs into the developer's repository, so they are **opt-in**. They run when a
capture project is configured, or when you ask for local-only snapshots:

| Setting | Meaning |
| --- | --- |
| `CCP_SNAPSHOT_REMOTE` | URL of the capture project for the repository |
| `CCP_SNAPSHOT_REMOTE_BASE` | Or a group URL: the project is `<base>/<repository name>-capture.git` (the name comes from the `origin` URL, else the folder name) |
| `CCP_SNAPSHOTS=true` | Take snapshots even without a remote: the refs stay in the local repository |

Set them in the `env` block of Claude Code's settings (see [hooks.md](hooks.md)). For now the capture
project can be a local bare repository (`git init --bare capture.git`); a real GitLab group comes with
the move to a server (the project must exist, and the machine needs non-interactive credentials).

## What a snapshot is

A hidden commit holding the files **as they are on disk right now**, on top of the developer's current
commit, named

```text
refs/claude-capture/sessions/<session_id>/0001      one per snapshot: 0001, 0002, ...
refs/claude-capture/users/<user>/heads/<branch>     a backup of each local branch tip
```

Its message records the session, sequence number, event, branch, the commit it started from, the time and
the excluded files:

```text
claude-capture snapshot

session: 4a64fad3-...
seq: 2
event: Stop
branch: main
started-from: 16bda89...
captured-at: 2026-11-13T10:15:02.114Z
excluded: config.py, .env.local
```

The first snapshot of a session (`event: SessionStart`) is the **baseline**: uncommitted work that already
existed. Later ones show what changed during the session. You can see Claude's changes between two
snapshots with `git diff <ref1> <ref2>`, or since the start with `git diff <started-from> <ref>`.

## When snapshots are taken

| Event | Action |
| --- | --- |
| `SessionStart` | Retry pushes that failed earlier; baseline snapshot (only if there is uncommitted work); back up all branches |
| `Stop` | Snapshot if the tree changed, **after** the transcript upload |
| `SessionEnd` | In the detached background flush: a final snapshot and a branch backup |

`UserPromptSubmit`, `PreCompact` and `SubagentStop` do not snapshot: the next `Stop` covers them.
A response that changes nothing creates no snapshot.

## How a snapshot is made, and why it is safe

1. A **temporary index file** is filled from `HEAD` and every working-tree file is added to it
   (`.gitignore` is respected). The developer's own index (their staged changes) is never read or changed.
2. **Files that contain a secret are excluded** (the secret rules of [redaction.md](redaction.md), not the
   personal-data rules: source code is full of email addresses). Such a file is reset to its committed
   version, or left out if it is new, and its name is recorded in the message.
3. If the resulting tree equals the last snapshot's, or the committed one, nothing is recorded.
4. Local commits that exist nowhere else are scanned for secrets too, because a snapshot is pushed together
   with the commits it builds on. A secret there keeps the snapshot **local** (`kept local`).
5. A commit is created on top of `HEAD` and a hidden ref points at it.

What is guaranteed, and tested (including that the developer's index file is byte-for-byte unchanged):

- the developer's `HEAD`, branches, staged changes and files are never modified;
- no secret reaches the capture project, no new branch is created, no temporary file is left behind;
- a Git problem never breaks a hook: it becomes a short reason in the log (Git's own output is never
  copied, because an error can contain a remote URL with credentials).

## Pushing, and when it fails

Snapshots are pushed to the capture project, which receives the snapshot **and every commit it builds on**
(including unpushed local commits), so comparisons inside a session need only the capture project.
A push that fails (no network, the project does not exist yet) is written to
`<state folder>/pending-pushes.jsonl` and retried at the next SessionStart. The queue holds only the
repository and the refs, **never the remote address** (it may contain credentials; it is looked up from the
current settings when retrying).

## What was ported from the first project

`reference/poc/test_poc.sh` had 27 checks. 23 apply and are ported one for one in
[`tests/unit/capture/snapshots/test_poc_port.py`](../tests/unit/capture/snapshots/test_poc_port.py), running the
same scenario in the same order. Four do not, by design:

| Old check | Why it no longer applies |
| --- | --- |
| Marker returned as additional context | The SessionStart hook must print nothing; the session id is known without a marker |
| Transcript uploaded (twice) and late transcript lines caught up | Transcripts are uploaded by the capture client (see [capture-client.md](capture-client.md)) |

Porting found a real Windows bug: Python's text mode turned `\n` into `\r\n` when sending data to Git, which
broke `git update-ref --stdin` (branch backups) and put carriage returns into commit messages. Input to Git is
now sent as bytes.

## Measured on a real repository

This project's own repository (151 files, no remote):

| | Time |
| --- | --- |
| First snapshot (including a scan of the whole history) | 2.4 s |
| A response that changed nothing | 1.0 s |

The repository (`HEAD`, status, the index file, the file list) was identical before and after, and no hidden
refs were left. Starting a Git process is slow on Windows; the number of Git calls per snapshot was cut by
batching the exclusions and merging the identity lookup. The `Stop` hook adds this time to every response.

## Limits

- **False positives.** 9 of those 151 files (6 %) were excluded as containing a secret. They are real
  matches of the generic `secret-assignment` rule (for example `CCP_INGEST_TOKEN=change-me-...` in
  `.env.example`, or code with a name like `token` or `password` next to a value). The rule is kept on
  purpose: a password written into a config file is the commonest leak. The cost is that such files are
  missing from the code history, and the excluded names are listed in the snapshot message. Reducing this
  (an entropy check, a per-repository allowlist) is on the plan.
- **A repository without a remote** counts its whole history as "unpushed", so a (fake) secret anywhere in
  the history keeps every snapshot local. The scanner has no allowlist for known-fake test values.
- **Per response, not per edit.** Several edits within one response appear as their end result; the
  transcript shows each one.
- **Hidden refs accumulate** in the developer's repository under `refs/claude-capture`; pruning is not built.
- **Very large repositories** are slower: every file is hashed on each snapshot.
- **Text files over 20 MB** cannot be scanned and are excluded.
- **One repository per session** (the one Claude works in); directories added with `/add-dir`, and Git
  worktrees created by sub-agents, are not snapshotted yet.
- **Credentials for a real GitLab** are not handled yet (no password prompt is ever shown; use an SSH key or a
  credential helper).

## Tests

- `tests/unit/capture/snapshots/`: the ported checks, 27 more (secrets in unpushed commits, empty and
  detached repositories, missing Git identity, deleted and binary files, the developer's index staying
  byte-identical, sequence numbers across processes), and the push queue and remote derivation.
- `tests/unit/capture/test_hooks_snapshots.py`: which events snapshot.
- `tests/integration/test_hooks_snapshots_end_to_end.py`: real hook processes, a real repository, a real
  capture project and the real API: a scripted session, the final snapshot after SessionEnd, the log line, a
  failed push retried at the next start, and "off unless configured".


## The capture chain (one labelled commit per change)

Snapshots made by the hooks ([hooks.md](hooks.md)) form a **chain**: one linear history per developer, machine and
branch, kept locally under `refs/claude-capture/chain/<user>/<host>/<branch>` and published to the capture project
as the branch `ccp/<user>/<host>/<branch>`, so GitHub and GitLab show it as a normal branch with the real project
layout. Each commit is **one change**, taken right after it happened:

| When | Hook | Who it is recorded as |
| --- | --- | --- |
| After an Edit, Write, MultiEdit or NotebookEdit call | PostToolUse | `ai_edit` |
| After a Bash or PowerShell call | PostToolUse | `ai_command` |
| After any tool call inside a sub-agent | PostToolUse | `subagent` |
| What changed since the last snapshot, when a prompt is sent | UserPromptSubmit | `human` |
| Changes seen at the end of a reply, or at session start and end | Stop, SessionStart, SessionEnd | `ai_other` / `human` |
| Quiet changes made while Claude is closed | the watcher ([below](#the-watcher-and-git-hooks)) | `human` |

A commit reads like this on GitHub (the author says who: `Claude (claude-opus-5-5) for <developer>`, or the
developer's own name for a person):

```text
[AI · claude-opus-5-5] Edit src/app.py

Why: add a retry to the upload step

session: 4a64fad3-...        (older lines kept: seq, event, branch, started-from, captured-at, excluded)
CCP-Actor: ai_edit
CCP-Developer: Dev A
CCP-Model: claude-opus-5-5
CCP-Session: 4a64fad3-...
CCP-Prompt: <uuid of the prompt>
CCP-Tool: Edit
CCP-Tool-Use: toolu_...
CCP-Agent: main
CCP-Event: PostToolUse
```

The prompt excerpt is redacted before it is written. Because each commit is one change, `git blame` on the chain
names the cause of every line, whoever made it: see `ccp-audit trace` in [audit.md](audit.md). The client also
reports each commit to the server (`POST /v1/sessions/{id}/snapshots`, table `shadow_commit`), which links it to
the chat, the tool call and the model. Tool hooks run only for the tools that can change files, and a snapshot
uses a cached index, so only changed files are read again; a lock stops parallel hooks from colliding.

### The watcher and Git hooks

`ccp-capture watch` (run it in the background; once per machine) looks at the repositories the hooks have seen. A
folder that has been quiet for 60 seconds with changes no snapshot has recorded gets a snapshot labelled `human`,
and every local branch, **unpushed commits included**, is kept backed up in the capture project. It waits while a
Claude tool call is running, so Claude's changes are never recorded as a person's. Options: `--interval`,
`--quiet`, `--once`. `ccp-capture watch-service install` starts it at every login (a systemd user unit on Linux, a launchd
agent on macOS, a Task Scheduler task on Windows; no administrator rights); `watch-service uninstall` removes it.

`ccp-capture git-hooks install` writes a `post-commit` and a `pre-push` script to `~/.ccp/git-hooks` and sets Git's
global `core.hooksPath` **only if it is not already set**. The scripts run silently (never failing the commit or
the push) and then run the repository's own hook of the same name. `post-commit` adds a Git note (`refs/notes/ccp`)
linking the commit to the capture chain; the commit itself is never changed. `pre-push` backs up the branches and
publishes the chain. `ccp-capture git-hooks uninstall` removes them.


### The secret check on source code

A file that contains a secret is left out of a snapshot (the committed version is kept, and the file name goes in the
`excluded:` line of the commit message). For transcripts the generic rule "value assigned to a name like password,
token or api_key" is deliberately greedy, but on source code it flagged `token = args.token`, `ENV_TOKEN =
"CCP_INGEST_TOKEN"`, a documented `https://user:token@host`, and placeholders, so ordinary files were left out. The
snapshot scan therefore ignores a value that is plainly not a secret: a function call, a code reference, a constant's
name, a path, a variable name or short plain word, `true/false`, or a placeholder (`change-me`, `REPLACE...`,
`example`, `invented`, `<...>`). Vendor key formats, private keys and other specific rules are unchanged, and a
long mixed value such as `password = "Sup3rS3cretValue!"` still excludes the file. On this repository the files left
out fell from 36 to about 14 of 375, mostly tests that contain invented secrets on purpose. Code:
`ccp.redaction.rules.SNAPSHOT_SECRET_RULES`.
