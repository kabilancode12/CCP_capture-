# Requirements tracker

The 21 requirements from the demo scope (R1 to R9, Thiagarajan) and the review (R10 to R17, Chakkaravarthy), plus the
items added since. Status as of 2026-11-16. **Done** means built and tested; "live" means also seen working in a real
Claude Code session.

| # | Requirement | Status | Where / what is left |
| --- | --- | --- | --- |
| 1 | Session start and end through hooks | Done (live) | [hooks.md](hooks.md); each opening is a session, ends are corrected from the transcript |
| 2 | Full chat transcript | Done (live) | raw, redacted, byte-exact in MinIO ([storage.md](storage.md)) |
| 3 | File changes, uncommitted and transient too, in a shadow Git | Done (live, headless CLI) | the capture chain, one labelled commit per change; the watcher catches work done while Claude is closed ([snapshots.md](snapshots.md)) |
| 4 | Deployable through settings.json and scripts | Done | `ccp-capture install` (hooks, thinking summaries), `watch-service install`, `git-hooks install` |
| 5 | CLI, desktop app and VS Code | Partial | transcript capture seen on the CLI and in VS Code; the new tool hooks were run live only on the Linux CLI; desktop app and JetBrains untested |
| 6 | Events beyond start and end | Done (live) | eight hooks, including before and after every file-changing tool call |
| 7 | Extraction tool: sessions, commits, files | Done | indexer, `ccp-audit`, Explorer, Metabase |
| 8 | Export a session to Markdown | Done | `report.md` in every chat folder, rewritten after every turn; `ccp-catalog export`; Explorer download ([catalog.md](catalog.md)) |
| 9 | Automated export and push to the knowledge base | Partial | the export is automatic and stored in MinIO; nothing pushes it to a separate knowledge base yet (Step 4) |
| 10 | Forward trace: chat to code change | Done | `ccp-audit snapshots`, Explorer *Changes by cause* tab, `file_change` and `shadow_commit` |
| 11 | Reverse trace: line to conversation; file history; git notes or hunk matching | Done | exact: `ccp-audit trace` (git blame on the capture chain, then prompt, thinking, decisions). Decision: the chain is the primary method; the hunk-matching `why` and `link-commits` stay for chats captured before it; a Git note on each developer commit (`post-commit` hook) links it to the chain ([audit.md](audit.md)) |
| 12 | Shadow repo mirrors the original file layout | Done (live) | the chain is published as a normal branch `ccp/<user>/<host>/<branch>` holding the real tree |
| 13 | Claude's reasoning and self-steering, incl. memory | Partial | thinking summaries are captured when `showThinkingSummaries` is on (the install sets it); plans, todo lists and memory or CLAUDE.md content that steered Claude are not captured as such |
| 14 | Test loop: generated, run, outcomes, how results steered the next code | Partial | test runs, outcomes, cases, tests added per edit, `test_fix` corrections, and manual checks against a local service are recorded; tests that existed before are not told apart from generated ones |
| 15 | Change attribution: user review, Claude self-review, test; human vs AI; "AI via command" | Partial | every change is `human`, `ai_edit`, `ai_command` or `subagent`, with the model; corrections record test, Claude and developer drivers; decisions record who decided and who reviewed (AI model or person) |
| 16 | Multi-agent capture: personas and delegation | Partial | sub-agent runs, their instructions and results, workflows, and changes made by a sub-agent are captured; named personas (coder, tester, reviewer, security auditor) are not a concept yet |
| 17 | Per-persona memory reviewable by human master reviewers | Pending | later phase; the Review page and decision overrides exist for decisions |
| 18 | Postgres as the structured store, shadow Git as the raw layer | Done | migrations 001 to 022 |
| 19 | Session lifecycle: creation, compaction, linking, memory | Partial | start, resume, clear, compact, end and idle are recorded; linking separate chats of one piece of work, and memory, are not |
| 20 | Per-session identity: Claude Code version, account | Done | `session_env`, `claude_account` |
| 21 | Per-session environment: surface, shell, OS, remote | Partial | collected except `run_mode` (interactive, headless, SDK); surface and access path are inferred |
| + | Approach library: search, compare, restore, lineage | Done | [approaches.md](approaches.md), Explorer, `ccp-mcp` ([mcp.md](mcp.md)) |

Known limits that apply across rows: the capture chain needs the hooks (and optionally the watcher) on every developer
machine; a file changed by a Claude shell command is recorded exactly by the chain but only partly by the older
transcript-based `file_change` rows; thinking text depends on the Claude Code setting above.
