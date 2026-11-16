# Decisions: what was decided in a chat, and who approved it

For each chat that has messages, the decisions step extracts the key decisions (the goal, the options
considered, what was chosen and why, the result, the tests, the commits and the evidence), has a second,
independent AI call review each one, and stores them in the `decision` table. A person can override any
decision later (in the Explorer, or with the command below). Every decision shows who reviewed it.

It reads the [index](indexer.md) (run `ccp-index run --once` first) and calls a model. Decisions are not
index data: `ccp-index rebuild` never deletes them (see [database.md](database.md)).

| Task | Command |
| --- | --- |
| Decisions of one chat (id or the start of it) | `python -m ccp.decisions run --chat <id>` |
| Every chat that has none yet or has new activity | `python -m ccp.decisions run --pending` |
| Redo, even when up to date | add `--force` |
| See how large each digest is, without calling a model | add `--dry-run` |
| List the decisions of a chat | `python -m ccp.decisions list --chat <id>` |
| A person approves or rejects one | `python -m ccp.decisions review <decision id> approve --by "Name" --note "why"` (or `reject`) |

`run` prints counts only (decisions, how many approved by AI, how many need review), never the text of a
chat. `list` prints the decision titles, status and who reviewed them.

## How it works

1. **Digest.** A compact text of the chat is built from the index: the developer's prompts and short
   excerpts of Claude's replies, slash commands, edits (one line per file and prompt, with the change and its role), file changes made by shell commands (`cat > file`, `sed -i`, `rm`, `mv`, `mkdir`, `dotnet new`; see below), test runs with
   their pass and fail counts, corrections found by the indexer, and the commits linked to the chat. Every
   line has a number (`[L12]`). The digest is cut to `CCP_DECISION_MAX_CHARS`: excerpts get shorter first,
   then replies are dropped, then the middle of the chat is left out. Rewound (abandoned) work is not listed.
2. **Extraction call.** The model returns strict JSON: `decisions` (title, goal, options, chosen, reason,
   result, tests, who decided, the numbers of the edit lines it changed, commit ids, evidence line numbers)
   and `developer_corrections` (prompt lines
   where the developer corrected Claude). The answer is checked: an invalid item is dropped; evidence
   numbers that are not in the digest are removed; commit ids that match no commit linked to the chat are
   removed; a correction must point at a developer prompt.
3. **Review call.** A separate call to a **different model** (`CCP_REVIEW_MODEL`), with its own
   instructions ("you did not write these"), gets the digest
   and the extracted decisions and answers `approve` or `needs_review` with a short note for each. A
   decision the reviewer gives no verdict for is stored as `needs_review`, never approved by default.
4. **Store.** Each decision gets `status` `approved` or `needs_review`, `reviewed_by_kind = 'ai'`,
   `reviewer` = the review model, `model` = the writing model, the review note, `decided_by`, `files`, `prompt_version`, and one entry in `review_history`. Evidence is
   kept as `evidence_lines`: `[{"key": "<raw object key>", "line": <line number>}]`, a pointer into the raw
   transcript in MinIO, which a rebuild of the index does not change. The developer corrections become
   `correction` rows (`kind` `developer_correction`, `corrected_by` `developer`, `detected_by` `ai`).
   Unlike decisions, these rows point at events, so a rebuild of the index removes them together with the
   events; `run --force` makes them again.

## Human override

A person can set a decision to `approved`, `rejected` or `needs_review` with a comment. The decision then
shows `reviewed_by_kind = 'human'` and the person's name, and the review is appended to `review_history`, a
list of `{at, by_kind, reviewer, status, note}` that is only ever added to. In code:
`ccp.decisions.set_decision_status(connection, decision_id, status, reviewer, note)`.

## What a decision looks like

Each decision is short and has the same fields. The instructions (prompt version 2) ask for:

| Field | Meaning |
| --- | --- |
| `title` | 2 to 6 words, a technical name, for example "Streaming CSV parser" |
| `goal` | one sentence: what was to be achieved |
| `options` | the alternatives that were really named in the chat, each as "name: main trade-off"; `[]` if none |
| `chosen` | one sentence naming the technique or tool chosen |
| `reason` | one or two sentences with the reason given in the chat |
| `result` | what worked, what failed, measured numbers, known limits |
| `tests` | for example "pytest: 12 passed, 0 failed"; "No tests run"; or what Claude reported, marked "(reported by Claude, not a recorded test run)" |
| `decided_by` | `developer` when the developer's own words set the direction, `claude` when Claude chose |
| `files` | the files the decision changed: `[{"path", "change": created/modified/deleted, "via": edit/shell}]` |

Routine and operational steps (a port fix, a restart, a failed cleanup) are not decisions. Anything the
model would have to infer is written as "not stated", and the reviewer sends a decision with an inferred
field to a person (`needs_review`). Review notes are plain words and never cite digest line numbers.

**Files are never written by the model.** The model only names the EDIT and SHELL digest lines that belong
to a decision; the file names, the kind of change and how it was made are read from the recorded data
(`file_change` for Claude's Edit and Write tools, and the commands read by `ccp.index.shellfiles` for
shell changes). The shell reader understands only clear cases (`>`, `>>`, `tee`, `sed -i`, `rm`, `mv`, `cp`,
`touch`, `mkdir`, project scaffolds such as `dotnet new`), skips `/tmp` and `/dev`, and gives no file for a
command it cannot read. Shell changes have no line counts.

## When a chat is processed

A chat is processed when it has no decisions yet, or when its last event is newer than its newest decision,
or with `--force`. Chats that are empty (`session.is_empty`) or have no developer prompt are never
processed. Processing a chat again replaces the decisions an AI wrote and keeps those a person reviewed (new
ones are numbered after them); the AI's earlier developer corrections are replaced too. Corrections found by
rules (`detected_by = 'rule'`) are never touched. Nothing is changed if a model call fails or its answer is
unusable, and if a run returns no valid decision the stored ones are kept.

A chat for which the model finds no decision at all has none stored, but it is marked as checked
(`session.decisions_checked_at`, migration 019): it is not sent to the model again until it has new activity.
A chat whose run failed is left alone by the server for 30 minutes, because every try costs tokens.

## The model

| Setting | Default | Meaning |
| --- | --- | --- |
| `CCP_DECISION_MODEL` | `claude-opus-5-5` | the model that writes the decisions |
| `CCP_REVIEW_MODEL` | `claude-sonnet-5-5` | a different model that reviews them (it checks the work with other blind spots) |
| `CCP_DECISION_TIMEOUT_SECONDS` | `600` | longest wait for one call |
| `CCP_DECISION_MAX_CHARS` | `80000` | largest digest sent to the model |

By default the `claude` command is used: `claude -p --output-format json --model <model>`, with the prompt on
standard input, in an empty temporary folder, with no tools, without saving the session, and with
`CCP_CAPTURE_DISABLED=1` so that the capture hooks do not record the extraction as a chat of its own (it
would otherwise be captured and analysed in turn). The command must be on the PATH and logged in.

If the `anthropic` package is installed (it is not a dependency of this project) and `ANTHROPIC_API_KEY` is
set, the Messages API is used instead, with adaptive thinking. Otherwise the command is used.

## Privacy

The digest contains excerpts of the (already redacted) conversation and file names, and it is sent to the
model. Use it only where sending chat content to the model is allowed. Tests never call a model: they use a
fake provider.
