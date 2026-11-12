# Test-session script

A step-by-step script for producing **one** Claude Code session that exercises every behaviour
the design depends on. Run it, then inspect the transcript with `ccp-verify` and record the
results in [transcript-facts.md](transcript-facts.md).

Use a scratch Git repository with no real code or secrets. Everything typed here ends up in the
transcript.

## Before you start

1. Create a scratch repository and open a terminal in it:

   ```powershell
   mkdir C:\temp\ccp-verify-repo; cd C:\temp\ccp-verify-repo
   git init; "def add(a, b):`n    return a - b" | Set-Content calc.py
   "from calc import add`n`ndef test_add():`n    assert add(2, 3) == 5" | Set-Content test_calc.py
   git add .; git commit -m "initial"
   ```

   (`calc.py` has a deliberate bug: `a - b`. `pip install pytest` if needed.)

2. Run `ccp-verify env --out env-before.json` and keep the file.
3. Note the Claude Code version: `claude --version`.

## Steps

Run `claude` in the scratch repository, then work through the steps in order. After each step,
note what you saw in the "Check" column.

| # | Do this | Why | Check |
| --- | --- | --- | --- |
| 1 | Turn on extended thinking, then ask: "Explain what calc.py does." | Thinking blocks | Is there a `thinking` block in the transcript? Does it hold text or only a signature? Any `redacted_thinking`? |
| 2 | Ask: "Run the tests, then fix the bug." | Test run that fails, an edit, then passes | Edit tool call present? Does its result carry `toolUseResult` with line ranges? |
| 3 | Ask: "Use a sub-agent to list every Python file and report back." | Sub-agent layout | Inline (`isSidechain`) or separate files? Is the parent's Task call linked to the result? |
| 4 | Press Esc twice (or `/rewind`) and go back to before step 2, then ask something different. | Rewind | How does the transcript show it? Entries with the same `parentUuid` (a branch)? Any checkpoint entry? |
| 5 | Type `/compact`. | Manual compaction | Which entry records it (`summary`, `compact_boundary`, `isCompactSummary`)? Is the trigger recorded? |
| 6 | Type `/exit`. | Session end | Did a SessionEnd hook run (if hooks are installed)? Is the last transcript line complete? |
| 7 | Run `claude --resume` and pick the same session; ask "What did we do?" | Resume | Is the session id the same, or is there a new one? Which file holds the new messages? |

## After the session

```powershell
# Find the transcript (Claude Code stores them under your home folder)
Get-ChildItem "$HOME\.claude\projects" -Recurse -Filter *.jsonl | Sort-Object LastWriteTime | Select-Object -Last 3

# Inspect it (include the folder to pick up any sub-agent files)
ccp-verify transcript "<path to the folder or file>" --out transcript-report.md
ccp-verify env --out env-after.json
```

Copy the findings into [transcript-facts.md](transcript-facts.md). Do **not** commit the
transcript or the report if it contains anything private; the report shows structure only,
but review it before sharing.

## Environment matrix

Repeat steps 1, 2 and 6 and the `env` check in each environment. Mark what you tested.

| Environment | Tested | Hooks fire? | Notes |
| --- | --- | --- | --- |
| Windows, Command Prompt | [ ] | | |
| Windows, PowerShell | [ ] | | |
| Windows, Git Bash | [ ] | | |
| Windows, WSL | [ ] | | |
| Linux terminal | [ ] | | |
| macOS terminal | [ ] | | |
| VS Code extension | [ ] | | |
| JetBrains plugin | [ ] | | |
| Desktop app (Code tab, local) | [ ] | | |
| Headless: `claude -p "hello"` | [ ] | | |
| SSH remote session | [ ] | | |
| Claude Code on the web | [ ] | | |
