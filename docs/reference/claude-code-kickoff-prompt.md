# Claude Code Kickoff Prompt — Session Capture & Knowledge Base

How to use: open Claude Code in the root of the capture POC repository (the one with `capture.sh`, `kb_index.py`, `test_poc.sh` and the handover docs). Start in **plan mode**, then paste the prompt below. Use the phase prompts at the end once each phase is approved.

---

## Kickoff prompt (Phase 0)

```
You are joining an existing project: capturing every Claude Code session our developers run,
storing it safely, turning it into structured data in Postgres, and building two outputs on top:
an audit trail and a knowledge base.

## Read first, then summarise back to me before changing anything
1. HANDOVER-claude-interaction-capture.md (design, decisions, POC state)
2. The "Next Steps and Capability Gaps" backlog (Parts A–C)
3. capture.sh, kb_index.py, test_poc.sh and any hook config in .claude/
Give me: what works today, what is partial, and anything in the code that contradicts
the docs. Treat the backlog as reference, not as orders.

## Agreed design decisions (do not change without asking me)
- Three layers: raw capture (complete, never lossy) -> index (rebuildable from raw)
  -> knowledge entries. Anything in layer 2/3 must be reproducible from layer 1.
- Storage split: code snapshots stay as Git refs; raw transcripts go to MinIO
  (object storage, deletable); all structured data goes to
  PostgreSQL 16 + pgvector, including the job queue. Raw transcripts never go into Git.
- Redaction (secrets + Indian personal data: Aadhaar, PAN, phone, email) happens on the
  developer machine BEFORE upload. Never write a real secret into any file, log,
  test fixture or chat output.
- Hooks: SessionStart, UserPromptSubmit, Stop, PreCompact, SubagentStop are the backbone.
  SessionEnd is best-effort only: nothing may depend on it finishing.
- Never modify developer commits or branches. Capture is read-only toward their work.
- Every event carries an actor: human | ai | subagent | system.
  File changes have three-way attribution: ai_edit | ai_command | human.
  "AI via command" (ai_command) is reported as its own class everywhere.
- Every session records who ran it and where: Claude Code version; account name,
  email, organisation, auth type (subscription or API key); OS user and hostname;
  run mode (interactive CLI, headless `claude -p`, Agent SDK app); surface (terminal,
  VS Code, JetBrains, Desktop app, Claude Code on the web); shell (bash, zsh, Git Bash,
  WSL, cmd, PowerShell); OS (Windows, Linux, macOS + version, arch); access path
  (local, SSH, dev container, remote/web session).
- Private mode: when on, keep only a minimal session record (id, developer, account,
  environment, start/end, private flag); upload no prompts, transcript or code.
  Every private-mode toggle is logged as an event.
- Commit linking (Phase 3): git notes attach session IDs to developer commits without
  modifying them; hunk matching is the fallback for commits without a note.
- Thinking/chain-of-thought is stored in a restricted table and labelled
  "summarised reasoning", never treated as proof of why the AI acted.
- Must work on Windows (Git Bash, CRLF) and Linux.

## Phase 0 task: verify before we build
Many design points depend on what Claude Code actually writes to transcripts.
Build tooling to establish the facts. Do NOT change capture behaviour yet.

1. scripts/verify/inspect_transcript.py
   Given one or more transcript JSONL files (and any sub-agent folders), print a
   Markdown inventory:
   - every entry `type` and the set of keys seen per type, with counts
   - assistant messages: are `model` and `usage` (input/output/cache tokens) present?
   - thinking blocks: present? text or signature only? any redacted_thinking?
   - Edit/Write/MultiEdit results: is there a structured patch (e.g. toolUseResult
     with line ranges)? show the shape with values masked
   - conversation tree: uuid/parentUuid present? any branches (rewinds)? checkpoint
     or file-history entries?
   - compaction: how is it recorded (summary entry, trigger)?
   - sub-agents: inline (isSidechain) or separate files? how are they linked to the
     parent Task call?
   - session start source / resume: same session id or new?
2b. scripts/verify/inspect_environment.py
   Run inside a hook or a shell and print, as JSON, everything that identifies the
   environment: Claude Code version, where account name/email/org/auth type can be read
   locally (report the location and field names; mask the values), run mode, surface,
   shell, OS + version + arch, and access path (SSH, container, remote/web markers).
   Mark every detection rule [verify] and list which surfaces you could not test.
   Mask all string values longer than 40 chars by default; show structure, not content.
2. docs/verify/test-session-script.md
   A step-by-step script a human runs in Claude Code to produce one test session that
   exercises: extended thinking, a sub-agent (Task), a test that fails then passes,
   a rewind, a manual /compact, exit and resume. Include what to check after each step.
   Add a matrix of environments to repeat the hook + environment check in: terminal on
   Windows (cmd, PowerShell, Git Bash, WSL), Linux and macOS; VS Code; JetBrains;
   Desktop app; headless `claude -p`; an SSH remote session; Claude Code on the web.
3. docs/verify/transcript-facts.md
   A template to fill from the inspector output, with one row per [verify] item:
   question | observed (Windows) | observed (Linux) | Claude Code version | impact on design.
4. infra/docker-compose.yml for local dev: postgres:16 with pgvector, and MinIO.
   No credentials committed; use a .env.example.
5. db/migrations/001_init.sql as a DRAFT (do not apply) for these tables:
   session (with private flag), session_env, session_link, event, model_usage,
   agent_run, thinking, file_change (actor class incl. ai_command), commit_link,
   test_run, context_snapshot, compaction, knowledge_entry (vector column),
   redaction_log, job. Use JSONB for raw payloads, keep a pointer from every
   event to its raw object key + line number, and mark fields that depend on
   [verify] items with a comment.
6. .gitignore entries so no transcript, .env or MinIO data can be committed.

## How to work
- Plan first: list files you will create or change and wait for my approval.
- Small, reviewable commits with clear messages. Add tests for the inspector
  using small synthetic JSONL fixtures you write yourself (no real transcripts).
- Mark every assumption about Claude Code behaviour as [verify] in code comments.
- If something in the existing code or docs conflicts with the decisions above,
  stop and ask instead of choosing silently.
- At the end, give me: what you built, how to run it, and the open questions.
```

---

## Phase prompts (use after each phase is approved)

### Phase 1 — Safe capture and ingestion

```
Phase 0 facts are in docs/verify/transcript-facts.md. Now build Phase 1.

1. Capture script (keep capture.sh's Git snapshot behaviour; add):
   - redaction module (gitleaks rules + Indian PII patterns) applied before upload,
     with a redaction_log record per hit (rule id, location; never the value)
   - incremental upload: track byte offset per transcript; upload only new lines
   - offline queue with retry; idempotent uploads (session id + offset)
   - hooks: SessionStart, UserPromptSubmit, Stop, PreCompact, SubagentStop;
     SessionEnd only triggers a best-effort flush, detached, never blocking exit
   - at SessionStart, collect the environment and account record (session_env)
   - private mode switch: minimal record only, toggles logged as events
2. Ingestion API (FastAPI): token auth, accepts chunks, writes to MinIO,
   upserts the session row, enqueues an index job in the Postgres job table.
   Server marks sessions closed after N minutes idle.
3. Tests: planted fake secrets and fake Aadhaar/PAN numbers must never reach MinIO;
   killing the network mid-session loses nothing once it returns.
Plan first, wait for approval.
```

### Phase 2 — Indexer and full event model

```
Phase 1 is running. Now evolve kb_index.py into the Phase 2 indexer.

Read raw JSONL from MinIO, write to the Postgres schema (apply and adjust
001_init.sql as a real migration). It must be fully re-runnable: drop and rebuild
a session's layer-2 rows from raw at any time.

Index: conversation tree (mark abandoned branches), actors, permission decisions,
plans and to-do lists, model usage and cost, sub-agent runs linked to the parent
Task call, thinking (restricted table), file changes with hunks and three-way
attribution (ai_edit / ai_command / human), test runs (pass/fail level), compactions,
session links (resume/continue/same branch), context snapshots (CLAUDE.md, memory,
agent definitions, settings) by content hash.

Done when these SQL questions have saved queries with tests:
- cost and models per session
- every change to a given file, with actor and the prompt behind it
- sub-agents in a session: instructions given and result returned
- human decisions taken (denials, rewinds, plan approvals)
- where a session compacted
- where tests failed and then passed after edits
Plan first, wait for approval.
```

### Phase 3 and 4 (outline only — refine when we get there)

- **Phase 3:** Metabase on Postgres (usage, cost, models, fix loops), split by environment and actor class (ai_edit, ai_command, human); `why <file> <line>` and `history <file>` commands built on file_change + event; commit linking via git notes with hunk-matching fallback, stored in commit_link. Output for client demo 1.
- **Phase 4:** value filter, LLM distillation into knowledge_entry with evidence links, pgvector search, a small search UI, and an MCP server so Claude Code can query the KB. Developer review flow. Output for client demo 2.
