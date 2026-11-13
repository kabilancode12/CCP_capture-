# Audit commands: file history and "why was this line written?"

`ccp-audit` answers questions about a file from the [index](indexer.md). It only reads: nothing is changed. Run
`ccp-index run --once` first so that the index is current. Settings are the `POSTGRES_*` variables
([configuration.md](configuration.md)). If the launcher is blocked, use `python -m ccp.audit`.

| Task | Command |
| --- | --- |
| Every recorded AI edit of a file, oldest first | `ccp-audit history <file>` |
| The same, including edits made on rewound branches | `ccp-audit history <file> --include-abandoned` |
| Which AI edit wrote a line, and the prompt behind it | `ccp-audit why <file> <line>` |

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

## Other questions

The five other audit questions are views: [queries.md](queries.md). `v_file_history` lists edits by path and
`ccp-audit history` is its command-line form.
