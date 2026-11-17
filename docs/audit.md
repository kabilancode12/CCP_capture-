# Audit commands: file history, "why" and commit linking

`ccp-audit` answers questions about a file from the [index](indexer.md). It only reads: nothing is changed. Run
`ccp-index run --once` first so that the index is current. Settings are the `POSTGRES_*` variables
([configuration.md](configuration.md)). If the launcher is blocked, use `python -m ccp.audit`.

| Task | Command |
| --- | --- |
| Every recorded AI edit of a file, oldest first | `ccp-audit history <file>` |
| The same, including edits made on rewound branches | `ccp-audit history <file> --include-abandoned` |
| Which AI edit wrote a line, and the prompt behind it | `ccp-audit why <file> <line>` |
| Link commits to the sessions that wrote their lines | `ccp-audit link-commits --repo <git directory>` |
| Which sessions produced a commit | `ccp-audit commit <sha>` |
| Which commits a session produced | `ccp-audit session-commits <session id>` |

`<file>` is a path relative to the session's folder (`src/app.py`) or an absolute path; either kind of slash
works. If the same relative path was edited in two projects, the command lists the full paths and asks for one.

```text
$ ccp-audit why src/ccp/audit/cli.py 41
Line 41 of src/ccp/audit/cli.py was last written by:
  2026-11-13 14:02:11  Edit  +3/-1  session 1528c746
  prompt: Add a why command that names the edit and its prompt

Based on the edits Claude Code recorded. Changes made by a person, by a shell command or before capture
are not visible.
```

## How `why` works

Each recorded edit carries the changed line ranges as a patch ([indexer.md](indexer.md#file-changes)). The
command **replays the file's edits in order**: a line an edit adds belongs to that edit, a line it leaves alone
keeps its earlier owner, and later edits shift lines up and down. The line number you give is a line of the file
**as it is after the last recorded edit**. The code is [`provenance.py`](../src/ccp/audit/provenance.py), a pure
function that is tested on its own.

Rewound (abandoned) edits are left out by default, because the code of a rewound branch may have been
restored. Which restore option was used is not recorded (an open question in
[verify/transcript-facts.md](verify/transcript-facts.md)), so `--include-abandoned` is offered for `history`
only.

## What the answer means, and what it cannot know

| Answer | Meaning |
| --- | --- |
| "last written by" an edit | That AI edit wrote the line, and no later recorded edit changed it |
| "No recorded AI edit wrote line N" | The line was written before capture started, by a person, or by a shell command (for example `sed`, a formatter or a script), or the line is beyond what the edits reveal |

The replay sees only what Claude Code recorded. **If the file was changed any other way**, the owners below that
change can be off by the number of lines that were added or removed. A formatter that rewrites the file, or a
`git checkout`, breaks the match. Task 3.4 adds the code snapshots to see such changes.

Checked on real edits from this project: for the 17 files whose line count still matched what the edits predicted,
**1,303 of the 1,308 lines** that the replay attributed to an edit had exactly the text on disk. The 26 other
files had been changed outside Claude Code (formatters, scripts), which is the limit described above.

## Commit linking

A developer's commit says nothing about Claude Code, but the lines it adds are, for AI-written code, the same
lines that a session's Edit and Write results recorded as added. `ccp-audit link-commits` compares them and
stores the result in `commit_link` (migration 012). The code is [`linking.py`](../src/ccp/audit/linking.py)
(the rule, pure and tested on its own) and [`gitlog.py`](../src/ccp/audit/gitlog.py) (reading Git).

```text
$ ccp-audit link-commits --repo /srv/capture/my-repo-capture.git
7aa4faf3c1  1528c746 78%
c28b3fb90e  1528c746 66%
...
35 of 38 commit(s) linked to a session.

$ ccp-audit commit 7aa4faf
7aa4faf3c1  session 1528c746-71a7-445b-a3af-0e6848e8d842  78% (matched of total lines)  started 2026-11-13 09:12  branch main
```

**Which commits are read.** By default the branch backups that the capture client pushes to the capture project
(`refs/claude-capture/users/*/heads/*`, see [snapshots.md](snapshots.md)), so developers' commits arrive there
without anything extra. `--ref PATTERN` (repeatable) reads other references, and `--all-refs` reads every one;
use that for an ordinary clone. Merge commits are skipped.

**The rule.** For each session, the confidence is the share of the commit's added lines that the session's
recorded edits also added: lines are compared by their text, stripped of space, within the same file (a commit's
path and an edit's path match when one is the tail of the other on a folder boundary). Lines shorter than 4
characters (`}`, a blank line) are ignored: they are everywhere. A line counts as often as the session added
it. Only edits made at or before the commit count (with 5 minutes of tolerance for clock differences). A commit
can link to several sessions, each with its own share; sessions below `--min-confidence` (default 0.3) are not
linked.

**A link is a likelihood, not a fact.** What it cannot do:

- A formatter that changes the lines after the AI wrote them lowers the confidence (on this project's own history
  the commits that came after `ruff format` have the lowest shares, 36 to 48 %).
- Identical code written by a person counts as written by the AI, and code that a person retyped is missed.
- A very small commit (one or two lines) may be below the threshold; that is by design.
- Commits made before capture started have no link.
- The comparison runs over every recorded AI edit at once, which is fine for a team's pilot and will need an index
  when the data is large.

Checked on this project: 35 of its 38 commits were linked to the session that made them, with shares between 36 %
and 100 %; a one-line typing fix was the clear miss.

Links made at commit time (a Git note written by a hook) are not built; the table has a `git_note` method
reserved for them.

## Other questions

The five other audit questions are views: [queries.md](queries.md). `v_file_history` lists edits by path and
`ccp-audit history` is its command-line form.
