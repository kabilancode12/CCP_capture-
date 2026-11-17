# Handover: Capturing Claude Interaction Context

**Owner:** Thiagarajan Ramani, Solutions Architect, Xenovex Technologies
**Purpose of this file:** complete context for the Claude Project, so that anyone (or any Claude
session) continuing this work knows the requirement, every decision and why it was made, the
state of the proposal, the reviewer feedback, the working POC, and what is still open.
**Classification:** Xenovex internal and confidential. Shared only with the project team.
**Status date:** 11 November 2026

How to use it: read sections 1 to 4 for the requirement and the design; section 5 for the
proposal document; section 6 for reviewer comments and replies; sections 7 to 9 for the POC
and how to run it; section 11 for what is open. Appendix A contains every script in full.

---

## 1. The client requirement

### 1.1 How it evolved

1. **Initial ask:** capture the interactions the client's teams have with Claude and store them
   as a knowledge base.
2. **Clarified goal (confirmed by Thiagarajan):** "reusable answers for other staff" **and**
   "not losing knowledge gained during the conversations". Not audit or usage analytics.
3. **Client also wants local Git commits and branches captured** alongside the interactions.
4. **Client confirmed:** it will move to **Claude Enterprise**; source code is on a
   **self-hosted GitLab**.

### 1.2 Client meeting notes (verbatim)

> Capturing Claude Prompt Engineering
> Goal: Capture the full context of Claude interactions to preserve valuable knowledge.
> Challenge: Claude's full context (prompts, files, intermediate states) is often in local Git
> branches, not the main repo.
> Proposed Solution: A system that transparently snapshots local Git branches, creating a
> complete history.
> Claude audit logs would then reference these specific Git versions.
> Alternative: Evaluate Claude's enterprise edition for native interaction history features.

### 1.3 Interpretation used in the proposal

- Prompts and responses live server-side and in Claude Code's local session files, not in Git.
  Files and intermediate states live in the working tree and local branches.
- "Alternative: evaluate Claude Enterprise" is not an alternative: Enterprise's Compliance API
  captures conversations but has no awareness of Git. Both capabilities are needed.

---

## 2. Product facts established (researched November 2026; re-verify before implementation)

### 2.1 Compliance API (Claude Enterprise only)
Docs: https://platform.claude.com/docs/en/manage-claude/compliance-sessions and
https://support.claude.com/en/articles/15167101

- Enterprise only (Team has audit logs in the UI but no API). Enabled by the **Primary Owner**
  (Organization settings > API). Needs a **Compliance Access Key**; an admin key only reads the
  activity feed.
- **Covers:** chats, files and projects (chat endpoints); sessions from Claude Code, Cowork,
  Claude for Microsoft 365; Claude in Chrome and Google Workspace add-ins (beta); activity feed.
- **Does not cover:** sessions using Console API keys; sessions through Amazon Bedrock, Google
  Vertex AI or Microsoft Foundry; Claude Code cloud sessions (including cloud scheduled tasks);
  zero-data-retention sessions; content from before the API was enabled (listed but "not captured").
- **Session identity:** a local session corresponds to one client session ID; a new conversation
  or clearing context starts a new record. Session IDs (`clls_...`) are opaque and **the client
  session ID is not returned**, so a direct join with Claude Code's session ID is not possible.
  Hence the **session marker** (section 3.4).
- **Truncation:** tool inputs and results truncated to 10,000 bytes by default; up to about
  1 MiB on request. Collectors must request the maximum.
- **Nothing is masked** (URLs, credentials, personal data). Redaction is our job.
- **Identity:** `user.email_address` becomes null when an account is deleted or leaves; capture
  the user-ID-to-email mapping at first sight.
- **Retention:** local session content kept 6 years by default, or the organisation's custom
  retention period if a finite one is set.
- **Polling:** use `updated_at.gte` with a few minutes of overlap, de-duplicate on `id`; cursors
  expire after 24 hours; handle rate limits and very large transcripts.
- Project instruction files (CLAUDE.md) appear as ordinary user-role content in transcripts,
  which is why hook-injected context (the marker) is expected to appear too.
- Anthropic positions these endpoints for **eDiscovery and DLP**. Using them for knowledge
  management needs written confirmation from Anthropic (pre-requisite P5).
- **Sub-agents:** not documented. The parent transcript always contains the instruction to the
  sub-agent and its returned result; whether inner steps appear in the parent or as separate,
  unlinked sessions is unknown. To be tested in Phase 1b and asked of Anthropic.
- **OpenTelemetry comparison (Anthropic's own table on that page):** OTel tool inputs are
  "truncated summaries"; tool results are metadata, with optional size-capped content capture
  for Claude Code; Anthropic recommends the Compliance API for session content. Size limits are
  applied when telemetry is produced, so sending it to a local file does not remove them.

### 2.2 Other Claude Enterprise features
- **Memory and Projects:** memory is per user and per project; off by default on Team and
  Enterprise until the owner enables it and each member turns it on. Not a shared org KB.
- **Enterprise search ("Ask [Org]")**: auto-provisioned project for Team and Enterprise; owner
  completes setup (Documents and Chat connectors required, Email optional; directory and custom
  MCP connectors can be added); system prompt managed by Anthropic; live MCP calls, no index;
  per-user credentials except custom connectors with a shared credential; Claude Desktop yes,
  mobile no; usage counts against limits. It does **not** search colleagues' Claude chats
  ("chat" means Slack/Teams). Right place to plug in the knowledge connector.
- **Custom data retention:** Enterprise only; Primary Owner/Owner sets it (Organization settings
  > Data and Privacy); default indefinite; minimum 30 days; months counted as 30 days; clock runs
  from last activity; **project retention overrides chat retention** (chats inside projects are
  kept indefinitely unless a project period is set); shortening deletes immediately and
  permanently (daily job); does not apply to standalone Claude Design, Claude Tag, Managed Agents
  or Claude Code in the cloud; flagged or legally required content may be retained; changes are
  audited. Implications: KB source links break after deletion, so entries must be self-contained;
  the KB copy outlives chat retention, so it needs its own lifecycle.
- **Skills:** organisation owners can provision skills org-wide; enabled by default but members
  can switch them off; scope to groups via plugins; requires code execution and Skills enabled.
  A Skill is behaviour only, not storage.
- **Microsoft 365 connector write tools** need Entra admin consent and org enablement.
- **Claude Fable 5:** not needed for this design (the hard parts are model-independent).
  Fable 5 and Mythos 5 access was suspended 12 June to 1 July 2026 under US export controls,
  then restored; another reason to stay model-agnostic.
- **Data export for individuals:** Settings > Privacy on web or desktop (not mobile); emailed
  link valid 24 hours. Team and Enterprise members can export their own chats only if an owner
  enables "Allow members to export their own data".

### 2.3 Claude Code hooks (docs: https://code.claude.com/docs/en/hooks)
- Hooks are entries in settings files (`~/.claude/settings.json`, project `.claude/settings.json`,
  or managed settings). Managed settings plus `allowManagedHooksOnly` enforce them centrally.
- Hook input on stdin includes `session_id`, `transcript_path`, `cwd`, `hook_event_name`.
- **SessionStart** can add context to the conversation (`hookSpecificOutput.additionalContext`).
  Text framed like system commands can trigger prompt-injection defences: keep the marker factual.
- **Exit code 2 on Stop** blocks Claude from finishing. The script always exits 0.
- **SessionEnd** hooks share a 1.5-second budget unless a per-hook `timeout` is set (up to 60 s).
- The **transcript file is written asynchronously** and may lag the conversation.
- The `args` (exec) form and `statusMessage` exist only in recent versions; the shell-form
  command works everywhere.
- Docs say hooks fire in the terminal, IDE extensions, the Desktop app and Claude Code on the
  web. Community bug reports say some events do not fire in the VS Code extension; one Windows
  report says Stop fires reliably there. **Must be tested** (section 11).

---

## 3. Solution design (final, as agreed)

### 3.1 Principles
- Raw transcripts are not knowledge (they include Claude's mistakes and abandoned attempts).
  Capture everything; curate before anything is presented as authoritative.
- Keep raw capture immutable; derive everything else from it so it can be rebuilt.
- The developer never has to do anything (no commits, no "save" action) and nothing they own is
  modified.

### 3.2 Capture layers
| Surface | Capture path | Phase |
| --- | --- | --- |
| Claude Code (terminal, likely IDE and Desktop local sessions) | **Client-side hooks**: transcript upload + code snapshots, keyed by Claude Code's session ID | 1a (no Enterprise needed) |
| Chat, Cowork, Microsoft 365, Chrome | **Compliance API** (only sanctioned route; no hooks exist for chat) | 1b (Enterprise) |
| Claude Code (second copy) | Compliance API copy, matched to the uploaded copy via the session marker, to detect missing or altered uploads | 1b |

Why hooks are primary for Claude Code: they already run to capture code; they see the full
transcript under the same session ID as the snapshots (no matching logic); they also capture
Console-API-key and third-party-cloud sessions. Weakness: the transcript is the developer's own
copy (could be edited before upload); the Compliance API copy is the independent check.

### 3.3 Code capture
- After every Claude response: snapshot the working tree (including uncommitted and new files)
  into a hidden commit on top of the developer's current commit, using a temporary index, so the
  developer's branch, HEAD, staged changes and files are untouched. Ignored files are excluded;
  files with detected secrets are excluded and the exclusion recorded.
- At session start: a **baseline snapshot** records uncommitted work that existed before the
  session, so it is not credited to the new session.
- Branch backup of every local branch (latest tip) at session start and end.
- Snapshots are pushed to **one capture project per source repository** (`<repo>-capture`) in a
  restricted GitLab group, created automatically for new repositories. Reasons: privacy of
  work-in-progress, tamper protection (server hook rejects deletes/rewrites except by the
  retention account), a clean main repository, separate retention. Forking may let GitLab share
  object storage (verify on the client's version).
- **Two repositories exist** (main and capture project), but comparisons within a session never
  need both: pushing a snapshot also pushes every commit it builds on (including unpushed local
  commits), so the capture project holds the full chain. Only "was this later merged?" uses both,
  and commit IDs are identical in both.
- Deletions: the next snapshot simply lacks the file; earlier snapshots still hold it; the
  transcript holds the delete command. Gaps: a file created and deleted within one response, an
  untracked file deleted before any snapshot, and ignored files. Per-edit snapshots (PostToolUse
  on Edit/Write) would close the first gap at the cost of many more snapshots.

### 3.4 Session marker
At SessionStart the hook adds a factual line to the conversation:
`Session capture reference for this conversation: capture_id=<uuid> repository=<origin or path>
branch=<branch> commit=<sha> developer=<git email>`. It records the starting commit in the
transcript itself and lets the Compliance API copy (whose IDs are opaque) be matched to the
uploaded copy.

### 3.5 Session definition (answer to reviewer comment #6)
- One Claude Code conversation from start until exit or clear; parallel instances are separate
  sessions.
- Sessions without a code base are captured conversation-only (transcript, tool calls, results).
- Sub-agents run inside the parent session (see 2.1 for what is unknown).
- If Claude is given additional directories, every Git repository in scope should be snapshotted
  (not yet implemented).
- Snapshots capture the whole working tree, so the developer's own edits in that repository are
  included; the transcript shows which changes Claude made. Repositories Claude never touches are
  covered by branch backup only.

### 3.6 Storage layers
| Layer | Holds | Where | Changes |
| --- | --- | --- | --- |
| 1 Raw capture | Transcripts (JSON lines), Git snapshot objects | Ingestion store / outbox; GitLab capture projects | Never |
| 2 Structured index | Sessions, messages, tool calls, snapshots, file changes, branch backups | PostgreSQL (SQLite in POC) | Rebuilt from layer 1 |
| 3 Knowledge base | Answers, decision records, artifact references with evidence links | PostgreSQL + pgvector | Grows through curation and review |

Layer 3 links to layer 2 by natural keys (session ID, message UUID, tool-use ID, snapshot commit)
so links survive rebuilds.

### 3.7 Knowledge base (Phase 2)
- Entry types: answer/how-to; decision record (context, options, decision, rationale,
  assumptions, owner, status, review trigger; linked as supersedes / depends on / affects);
  artifact reference (document, template or code change with summary).
- Every entry: source session, author, reviewer, status (draft, approved, superseded), code
  version where relevant, review-by date.
- Curation pipeline: redact > triage (cheap model) > evidence check (tests passing, change
  surviving into a commit or merge, user confirmation, no recurrence; code snapshots make this
  stronger) > distil (capable model; include diff from start commit to final snapshot) >
  de-duplicate and cluster > **author confirmation** > **expert approval** > publish.
  Unreviewed candidates expire.
- Serving: custom knowledge connector (MCP, per-user sign-in via identity provider) + an
  organisation-wide Skill (search approved knowledge first, cite entries, offer to submit when a
  conversation reaches a confirmed outcome) + the connector added to Ask Your Org.
- Freshness: entries tied to code flagged when that code changes; user feedback; review-by dates.
- Claude's role with decisions is advisory (flag conflicts with earlier decisions), not
  decision-maker. Discussed in context of public "Claude runs the company" experiments: in the
  documented ones a human still approves; Anthropic's Project Vend showed the risks.

### 3.8 Governance points
- Employee notice and consent covering capture **and** knowledge reuse (DPDP Act 2023) before
  any capture, including the pilot.
- Pilot developers must use company-controlled Claude accounts.
- Raw store access restricted; Compliance Access Key in a secrets manager, rotated, its use
  monitored via the Activity Feed.
- Private-session marking and exclusion of sensitive areas (HR, legal) from curation.
- Separate retention for raw capture versus approved knowledge.

### 3.9 Options considered and set aside
- Compliance API only (no Git awareness; misses API-key and third-party-cloud sessions; waits
  for Enterprise).
- Custom branch-snapshot agent (client's original idea; reinvents Git; no link to sessions).
- OpenTelemetry only (truncated/size-capped content; recommended only as optional metadata).
- Third-party archival (Smarsh, Proofpoint, Mimecast, Relativity: archival only, costly).
- Native memory and Projects only (per user; manual curation).
- Browser-extension scraping or session-cookie access to claude.ai for chat capture: rejected as
  unsupported, fragile and a credential risk.

---

## 4. Phasing (as in the proposal)

| Stage | Needs Enterprise | Scope | Duration |
| --- | --- | --- | --- |
| Phase 1a: Claude Code capture | No | Hooks (transcript + snapshots), capture platform, groundwork | ~4 weeks |
| Phase 1b: Enterprise capture | Yes | Compliance API for chat, Cowork, M365, Chrome; cross-check; enforced sign-in | ~3 weeks after Enterprise is live |
| Phase 2: Knowledge base | Partly | Curation, review, Knowledge Store, connector; Skill and Ask Your Org after 1b | ~6 weeks; development can start in 1a |

Acceptance highlights: 1a: transcript plus start commit, intermediate and final snapshots linked
by session ID; applying transcript edits to the start commit reproduces the final snapshot;
branches present; resilience; no secrets. 1b: stored transcripts match; Compliance API copy
matches hook copy; coverage. 2: a staff member not in the original conversation gets an answer
from an approved entry with links to source session and code version.

---

## 5. The proposal document

- **Live document (Claude Doc):** https://claude.ai/artifact/SaCyNwRX3sproTTaFUtoTV
  Title "Capturing Claude Interaction Context — Proposed Approach". Edits are made there and it
  can be exported to Word or PDF. **The Word file `Capturing_Claude_Interaction_Context_Proposal_V1_0.docx`
  (with the reviewer comments) is an older, separate copy**; none of the later changes are in it.
  Plan: export the doc as V1.1 and send it with the comment replies.
- **Diagrams:** `claude-context-capture-diagrams.drawio` (4 pages, connectors routed so no line
  crosses a component; bend points fixed): 1 Conversations architecture, 2 Conversations activity,
  3 Claude Code architecture, 4 Claude Code activity. Embedded in the doc as Figures 1 to 4.
  Figures 3 and 4 were regenerated for hook-based transcript capture and the cross-check.
- **Structure:** Document control; Executive summary; Key requirements (stated R1 to R6:
  full context, local branches, Git versions referenced, no loss when people leave, knowledge
  reuse, evaluate Enterprise; derived R7 to R14: automatic and enforced, secrets excluded and
  redacted, notice and consent, access-controlled and tamper-resistant, never interrupts the
  developer, human validation, traceable and current, separate retention); Proposed solution
  (components, why this combination, options set aside); Scenario 1 conversations; Scenario 2
  Claude Code (including "What counts as a session" and GitLab capture projects); Phase 2
  knowledge base; Security, privacy and governance (with retention table); Pre-requisites
  (P1 to P7, T1 to T14); Implementation plan (1a, 1b, 2 with acceptance criteria); Assumptions
  (8) and Open points for discussion (8).
- **Writing rules agreed for client documents:** no code snippets; mature tone; use
  "Assumptions and Open Points for Discussion" (never "to be confirmed by client").

### 5.1 Known gaps in the doc (not yet fixed)
1. Scenario 1 text and Figures 1 and 2 still say Claude Code is captured via the Compliance API
   (now hooks primary, API as cross-check).
2. The snapshot bullet in Scenario 2 should be reworded per reviewer comment #7 (section 6):
   "records the current state of every file in the repository as a hidden snapshot, including
   changes the developer has not committed. The developer does not need to commit anything; Git's
   commit format is used only to store the snapshot, outside the developer's branches. The
   developer's own commits and staged changes are untouched, and files the repository ignores
   (such as environment and key files) are excluded;"
3. Add "snapshot after every file change" as an open point (level of detail).
4. Add the baseline snapshot (session start) to Scenario 2.
5. Add the surface coverage table (section 10) once tested.

---

## 6. Reviewer comments and final replies

Reviewer: **Chakkaravarthy Periyasamy Balaiah ("Chakra")**, on the V1.0 Word file (4 to 5 Nov),
plus one later comment. None replied to or resolved in the file yet.

**#1 (Proposed solution):** "If we have to build a lot, we may as well not use the Enterprise
Compliance API. Looks like it's very limited compared to what we intend to capture."
Reply (update for hooks-primary design): *The Compliance API is the part we don't build. For
Claude Code we now capture client-side through hooks, because they also capture code; but chat,
Cowork, Microsoft 365 and Chrome have no hooks, so the Compliance API is the only sanctioned way to
capture them, and it gives an independent copy of Claude Code sessions to verify uploads. I'll add
a coverage table.*

**#2 (Components):** "How about troubleshooting sessions? ... capture the tool calls and tool
results. There's no code base here. We still need to sessionize the conversation."
Reply: *Covered. Transcripts include tool calls and results in full. Each Claude Code session is
one record with start and end. Sessions outside a repository are captured conversation-only
(marker recorded, no snapshot). Added as "What counts as a session".*

**#3 (OpenTelemetry row):** "If OTel target is another local file or directory, the truncation
can be avoided."
Final reply (after checking Anthropic's comparison table): *Anthropic's own table lists OTel tool
inputs as truncated summaries and tool results as metadata, with optional size-capped content for
Claude Code; the limits are applied when Claude Code produces the telemetry, so the destination
doesn't change them, and a local file also stays on the laptop. Anthropic recommends the Compliance
API for session content. We keep OTel as an optional add-on for device metadata and token usage.*
(Note: an earlier draft conceded the point; that was wrong and has been reversed. The doc row now
matches this reply.)

**#4 (Third-party archival):** "Agreed." Reply: *Thanks, no change.*

**#5 (Snapshot flow):** "More explanation is required. How can we check for the changes against
two repos - the main repo and the audit repo?"
Reply: *Yes, there are two repositories: the main one, unchanged, and a separate capture project
per repository. Comparing changes within a session doesn't need both: when a snapshot is pushed,
Git also copies every earlier commit it builds on, so the capture project holds the full history
and start-to-finish comparisons happen there. Checking whether work was later merged uses both,
which is straightforward because a commit has the same ID in each. The first push copies the
repository's history, so we'll look at creating capture projects as forks to share storage.*
(Demonstrated in the POC: the capture project held `initial` without anyone pushing it there.)

**#6 (Curation, "each session"):** "How do we identify a 'session'? ... multiple sub-agents running
in parallel ... working on multiple repos even while claude is working on one."
Reply: the session definition in section 3.5, with sub-agent recording in the Compliance API to be
confirmed in Phase 1b.

**#7 (later, on the snapshot bullet):** "That's possible only if the file changes during every turn
of the conversation are committed. That is not always the case."
Reply: *No commit is needed from the developer. After each Claude response the hook copies the files
as they are on disk, including uncommitted changes, into a hidden snapshot in the capture project;
Git's commit format is only the container. Two limits: snapshots are per response, so several edits
within one response are captured as their end result (the transcript still shows each edit; per-edit
snapshots are possible); changes to ignored files or files outside the repository appear only in the
transcript. I'll reword the section.* (Proven in the POC: uncommitted changes captured, nothing
committed for the developer.)

---

## 7. The POC (claude-capture-poc)

### 7.1 Files
| File | Purpose |
| --- | --- |
| `capture.sh` | The hook worker (bash; git; Python as python3, python or py; gitleaks if installed) |
| `settings.windows.example.json` | **Verified Windows hook registration** (Git Bash by full path, shell form) plus `env` |
| `settings.example.json` | macOS/Linux registration |
| `install.sh` | macOS/Linux installer; `--apply` merges hooks into `~/.claude/settings.json` safely |
| `kb_index.py` | Builds the structured index (layer 2) and empty knowledge tables (layer 3); report, export, query |
| `test_poc.sh` | 27 end-to-end checks of capture (simulated hook input) |
| `test_index.sh` | 19 end-to-end checks of the index (real snapshots + Claude Code-format transcripts) |
| `README.md` | How to run, configuration, Windows setup, limits |
| `CLAUDE.md` | Short context Claude Code loads automatically |

### 7.2 What capture.sh does
- Modes: `session-start`, `snapshot` (Stop), `session-end`. Reads hook JSON from stdin.
- **session-start:** generate capture ID; output the marker as additionalContext; baseline
  snapshot; re-copy transcripts of earlier sessions (lag/abrupt end); retry queued pushes; branch
  backup.
- **snapshot:** copy transcript (and a sub-agent folder beside it if present) to the outbox;
  temporary index; `git add -A` (respects .gitignore); secret scan of changed files (excluded files
  reset to HEAD version, recorded); skip if tree unchanged since last snapshot, or (first snapshot)
  identical to HEAD; scan unpushed commits (if secrets: keep snapshot local, log); `commit-tree` on
  HEAD with message fields (session, capture-id, seq, event, branch, started-from, captured-at in
  milliseconds, excluded); hidden ref; push (background, or synchronous with CAPTURE_SYNC=1; failures
  queued).
- **session-end:** transcript, snapshot, branch backup.
- Config: `CAPTURE_REMOTE` or `CAPTURE_REMOTE_BASE` (derives `<base>/<repo>-capture.git` from the
  origin name, or folder name if no origin), `CAPTURE_HOME` (default `~/.claude-capture`),
  `CAPTURE_OUTBOX`, `CAPTURE_SYNC`. Log: `~/.claude-capture/capture.log`.
- Always exits 0. Windows paths with backslashes are normalised.

### 7.3 What kb_index.py does
- `build [--capture PATH ...] [--outbox DIR] [--db PATH]`: rebuild layer 2 from the outbox and
  capture projects (bare or normal repos, or folders of them; repeatable; de-duplicates). Default DB
  `~/.claude-capture/capture-index.db`.
- Tables: repository, developer, session, message (kind: prompt, response, tool_result, marker;
  is_sidechain for sub-agents), tool_call (file_path, command, input, result, is_error), snapshot
  (is_baseline), file_change (added/modified/deleted, lines), branch_backup. Views:
  `v_session_overview`, `v_timeline`, `v_file_history` (each change with the preceding prompt).
- Layer 3 tables: knowledge_entry, evidence_link, code_reference, decision_link, review, feedback.
- `report`: sessions; files changed with the prompt that caused them; tools used; sessions with no
  code change (knowledge candidates). `export <session-prefix> --out file.md`: readable session.
  `query "SQL"`.
- Parser is tolerant of Claude Code's JSON lines (string or block content, tool_use/tool_result,
  summary lines, isSidechain). **Not yet run on real transcripts.**

### 7.4 Bugs found and fixed through testing
- Prompt-to-change linking wrong when in the same second: snapshot time now in milliseconds.
- A new session was credited with earlier uncommitted work: baseline snapshot at session start.
- Duplicate rows when the same snapshot is loaded from two places: de-duplicated.
- Windows: Python fallback (python3/python/py); backslash path normalisation; UTF-8 console output.

### 7.5 POC limits (deliberate)
Outbox instead of an ingestion service; basic regex secret scan without gitleaks; one repository
per session (no /add-dir); branch backup keeps latest tip only; per-response not per-edit snapshots;
sub-agent transcript layout unverified; local hidden refs not pruned; managed-settings deployment
not packaged; Windows tested on Windows 11 only.

---

## 8. Windows setup runbook (verified) and lessons learned

### 8.1 Steps
1. Prerequisites: `claude --version`, `git --version`, `& "C:\Program Files\Git\bin\bash.exe" --version`,
   `python --version`. **Update Claude Code first** (`claude update`, or
   `irm https://claude.ai/install.ps1 | iex`).
2. `New-Item -ItemType Directory -Force "$HOME\.claude-capture\bin"` then
   `Copy-Item .\capture.sh "$HOME\.claude-capture\bin\capture.sh"`.
3. `notepad "$HOME\.claude\settings.json"`: paste `settings.windows.example.json`, replace
   `YOUR-NAME` and `CAPTURE_REMOTE_BASE`. Save in Notepad.
4. Validate: `Get-Content "$HOME\.claude\settings.json" -Raw | ConvertFrom-Json | Out-Null; "valid"`.
5. Create the capture project for a test repo, e.g.
   `git init --bare C:\temp\capture-projects\capture-test-capture.git`.
6. Window 1: `Get-Content "$HOME\.claude-capture\capture.log" -Wait -Tail 50`.
   Optional: `while ($true) { Clear-Host; git for-each-ref refs/claude-capture; Start-Sleep 2 }`.
7. Window 2: `cd <repo>`; `claude`; `/hooks` should show "3 Hooks"; log shows `[session-start]`.
8. Ask Claude to change a file; log shows `[snapshot] ... pushed ...`.
9. Inspect: `git -C <capture.git> for-each-ref refs/claude-capture`,
   `git -C <capture.git> log --oneline <ref>`, `git -C <capture.git> show <ref>:<file>`,
   `git -C <capture.git> archive --format=zip -o C:\temp\snapshot.zip <ref>` (files as a zip), or
   `git -C <capture.git> worktree add --detach C:\temp\inspect-snapshot <ref>`.
10. Ask Claude "What session capture reference do you have for this conversation?" to confirm
    the marker.

### 8.2 Lessons (each cost time; do not repeat)
- `install.sh` is bash-only: register hooks by hand on Windows.
- Plain `bash` resolves to `C:\Windows\System32\bash.exe` (WSL), which cannot see `C:/` paths:
  error "/bin/bash: C:/Users/...capture.sh: No such file or directory". Call Git Bash by full path.
- Claude Code 2.0.76 did not understand the `args` field: use the shell-form command.
- Claude Code 2.0.76 failed sign-in with "OAuth error: Request failed with status code 404" after
  a 401 "OAuth access token has expired": fixed by updating Claude Code.
- `/hooks` shows the event list; "3 Hooks" at the top means registration worked; scroll to Stop.
- `claude --debug` writes a log under `%USERPROFILE%\.claude\debug\` with hook errors.
- Settings `env` values do reach hooks (CAPTURE_REMOTE_BASE worked).
- PowerShell 5 `Out-File` writes UTF-16 (shows as `<FF><FE>c^@h^@...` in git); harmless.
- Capture projects are bare repositories: no visible files; use show/archive/worktree.
- A snapshot is skipped when nothing changed since the last one (expected, not an error).
- PowerShell equivalent of `tail -f`: `Get-Content <file> -Wait -Tail 50`.

### 8.3 Live results recorded (Windows 11, updated Claude Code)
- Manual run: snapshot `manual-test/0001` captured the uncommitted change; staged file stayed
  staged; history unchanged; `0002` sat on top of `initial` (`16bda89`).
- Marker reached Claude: capture_id `45ef0389-be81-4cbe-a471-fcdb42e3289a`, repository
  `C:/temp/capture-test`, branch `master`, commit `16bda89...` (initial), developer
  `thiagarajan.r@xenovex.com`.
- Hook snapshot after "create hello.py": session `a15be34d-757d-46cd-883f-2dc76b4bd731`,
  snapshot `b0d09a0...`; transcripts in the outbox.
- Pushed to stand-in capture project `C:\temp\capture-projects\capture-test-capture.git`:
  snapshot `725c7a6` on `16bda89 initial`.
- Not yet confirmed live: SessionEnd (Step 7), kb_index.py on real transcripts, real GitLab.

---

## 9. Viewing what was captured
- Conversation: `~/.claude-capture/outbox/<session-id>/transcript.jsonl` (raw JSON lines).
- Code: hidden refs in the repo and the capture project (`refs/claude-capture/...`).
- Log: `~/.claude-capture/capture.log`.
- Claude Code's own transcripts: `~/.claude/projects/`.
- Structured view: `python kb_index.py build --capture <capture group folder> --capture <repo>`,
  then `report` and `export`.
- Claude.ai chats are not captured by the POC (no hooks); use the account export or, at
  organisation scale, the Compliance API.

---

## 10. Surface coverage (to be confirmed by test)
| Where | Expected |
| --- | --- |
| Claude Code terminal | Works (tested) |
| VS Code extension | Stop likely works; SessionStart/End unconfirmed: test |
| Desktop app Code tab, local session | Should work: test |
| Desktop app Code tab, cloud session | No (runs on Anthropic's machines) |
| Claude chat (web, desktop, mobile) | No hooks: Compliance API in 1b |
| Cowork | Unknown: verify |

If SessionStart does not fire somewhere, Stop still uploads the transcript and snapshots under the
same session ID; only the marker and baseline are missing.

---

## 11. Open items and next steps

### POC
1. Step 7: end a session with `/exit`; confirm `[session-end]` lines, final snapshot, pushes.
2. Run `kb_index.py` on the real transcripts; fix parser gaps (marker line type; sub-agent layout).
3. Test VS Code extension and Desktop app Code tab (local session); fill section 10.
4. Real GitLab: create group and `<repo>-capture` project; set CAPTURE_REMOTE_BASE to the group URL;
   non-interactive credentials (SSH key or credential manager); server-side protection hook;
   automatic capture-project creation.
5. Switch CAPTURE_SYNC off to test background pushing.
6. Replace outbox with authenticated, incremental upload to an ingestion endpoint.
7. gitleaks; multi-repo (/add-dir); optional per-edit snapshots; prune local refs; managed-settings
   packaging with allowManagedHooksOnly.
8. A readable-Markdown export is done (`kb_index.py export`); next is a prototype curation step.

### Proposal and client
1. Fix the doc gaps in 5.1; export V1.1 (Word) and send with the comment replies (section 6).
2. Questions for Anthropic: knowledge-management use of the Compliance API; how sub-agent work
   is recorded.
3. Client decisions: branch-history depth (latest tip vs every version); work outside Claude
   sessions (Git commit-time backup); retention periods; access to captured material; pilot team
   and knowledge domains; private-session mechanism and excluded areas; existing documentation
   platform; optional OpenTelemetry metadata; per-edit vs per-response snapshots.
4. Pre-requisites still open: Enterprise live and Primary Owner; employee notice; company accounts
   for the pilot; Anthropic confirmation; subject-matter expert owners.

---

## 12. Working preferences for this project
- Direct, substantive engagement and honest pushback over validation.
- Client documents: no code snippets; mature phrasing; "Assumptions and Open Points for
  Discussion".
- Structured deliverables that drop into governance (Word, draw.io, Excel, PowerPoint); ask before
  generating files from analysis.
- Work methodically one verified step at a time when testing.

## 13. Glossary
- **Capture project:** separate GitLab repository per source repository holding snapshots and
  branch backups.
- **Snapshot:** hidden commit of the working tree after a Claude response.
- **Baseline snapshot:** first snapshot at session start; pre-existing uncommitted work.
- **Session marker:** factual line injected at session start (capture ID, repo, branch, commit,
  developer).
- **Outbox:** local folder standing in for the ingestion endpoint.
- **Layer 1/2/3:** raw capture / structured index / knowledge base.
- **Phase 1a/1b/2:** Claude Code capture / Enterprise capture / knowledge base.

---

## Appendix A: complete file contents

The same files are supplied separately in `claude-capture-poc.zip`. They are reproduced here so
that this document alone is enough to restore the POC.

### A.1 `capture.sh`

~~~~bash
#!/usr/bin/env bash
# claude-capture POC: Claude Code hook script.
#
# Usage (wired up in Claude Code settings, see settings.example.json):
#   capture.sh session-start   # SessionStart hook
# Requires bash (Git Bash on Windows), git and Python (python3, python or py).
#   capture.sh snapshot        # Stop hook (after each Claude response)
#   capture.sh session-end     # SessionEnd hook
#
# Claude Code passes the hook input as JSON on stdin
# (session_id, transcript_path, cwd, hook_event_name, ...).
#
# Configuration (environment variables):
#   CAPTURE_REMOTE         Git URL of the capture project for this repository.
#   CAPTURE_REMOTE_BASE    Alternative: base URL of the capture group; the capture project is
#                          derived as <base>/<repository name>-capture.git
#                          (e.g. git@gitlab.example.com:claude-capture).
#                          Neither set = keep snapshots local only.
#   CAPTURE_HOME           State, logs and outbox. Default: ~/.claude-capture
#   CAPTURE_OUTBOX         Where transcripts are "uploaded". Default: $CAPTURE_HOME/outbox
#                          (stands in for the ingestion endpoint in this POC)
#   CAPTURE_SYNC=1         Push synchronously (used by the test). Default: background push.
#
# Design rules:
#   - Never block or fail the developer's session: every path exits 0.
#   - Never touch the developer's branch, HEAD, index (staged changes) or files.
#   - Respect .gitignore; scan for secrets before anything leaves the machine.

set -u

MODE="${1:-}"
CAPTURE_HOME="${CAPTURE_HOME:-$HOME/.claude-capture}"
CAPTURE_OUTBOX="${CAPTURE_OUTBOX:-$CAPTURE_HOME/outbox}"
CAPTURE_REMOTE="${CAPTURE_REMOTE:-}"
CAPTURE_REMOTE_BASE="${CAPTURE_REMOTE_BASE:-}"
REF_NS="refs/claude-capture"
LOG="$CAPTURE_HOME/capture.log"

mkdir -p "$CAPTURE_HOME/state" "$CAPTURE_OUTBOX" 2>/dev/null

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$MODE" "$*" >> "$LOG" 2>/dev/null; }
finish() { exit 0; }

INPUT="$(cat 2>/dev/null || true)"

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json' >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && { log "no working Python found (tried python3, python, py); nothing captured"; exit 0; }

json_get() {
  # json_get <key>  -> prints value of a top-level key from the hook input, or empty
  INPUT_JSON="$INPUT" "$PY" -c '
import json, os, sys
try:
    d = json.loads(os.environ.get("INPUT_JSON") or "{}")
    v = d.get(sys.argv[1], "")
    print(v if isinstance(v, str) else json.dumps(v))
except Exception:
    print("")
' "$1" 2>/dev/null
}

SESSION_ID="$(json_get session_id)"
TRANSCRIPT="$(json_get transcript_path)"
CWD="$(json_get cwd)"
EVENT="$(json_get hook_event_name)"
[ -z "$SESSION_ID" ] && { log "no session_id in hook input; nothing to do"; finish; }
[ -z "$CWD" ] && CWD="$PWD"
TRANSCRIPT="${TRANSCRIPT//\\//}"; CWD="${CWD//\\//}"

STATE="$CAPTURE_HOME/state/$SESSION_ID"
mkdir -p "$STATE"
[ -n "$TRANSCRIPT" ] && echo "$TRANSCRIPT" > "$STATE/transcript_path"

# ---------------------------------------------------------------- helpers

in_repo() { git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; }

resolve_remote() {
  # Use CAPTURE_REMOTE, or derive the capture project from CAPTURE_REMOTE_BASE.
  [ -n "$CAPTURE_REMOTE" ] && return 0
  [ -n "$CAPTURE_REMOTE_BASE" ] && in_repo || return 0
  local origin name
  origin="$(git -C "$CWD" remote get-url origin 2>/dev/null || git -C "$CWD" rev-parse --show-toplevel)"
  name="$(basename "$origin")"; name="${name%.git}"
  CAPTURE_REMOTE="${CAPTURE_REMOTE_BASE%/}/${name}-capture.git"
}

repo_root() { git -C "$CWD" rev-parse --show-toplevel 2>/dev/null; }

user_slug() {
  local e
  e="$(git -C "$CWD" config user.email 2>/dev/null || whoami)"
  printf '%s' "$e" | tr -c 'A-Za-z0-9._-' '_'
}

# Built-in secret patterns; gitleaks is used instead when installed.
SECRET_RE='AKIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{36}|xox[abprs]-[A-Za-z0-9-]{10,}|(api[_-]?key|secret|token|passw(or)?d)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9/+_.-]{12,}'

file_has_secret() {
  # file_has_secret <path>  -> exit 0 if a secret is detected
  local f="$1"
  [ -f "$f" ] || return 1
  if command -v gitleaks >/dev/null 2>&1; then
    gitleaks detect --no-git --no-banner --source "$f" >/dev/null 2>&1 && return 1 || return 0
  fi
  grep -I -E -q -i -- "$SECRET_RE" "$f" 2>/dev/null
}

push_ref() {
  # push_ref <local ref> [force]  -> push one ref to the capture project, queue on failure
  local ref="$1" force="${2:-}"
  local root; root="$(repo_root)"
  if [ -z "$CAPTURE_REMOTE" ]; then
    log "CAPTURE_REMOTE not set; kept $ref locally"
    return 0
  fi
  local spec="$ref:$ref"; [ -n "$force" ] && spec="+$spec"
  if [ "${CAPTURE_SYNC:-}" = "1" ]; then
    git -C "$root" push -q "$CAPTURE_REMOTE" "$spec" >/dev/null 2>&1 \
      && log "pushed $ref" \
      || { echo "$root|$spec" >> "$CAPTURE_HOME/pending"; log "push failed, queued $ref"; }
  else
    ( git -C "$root" push -q "$CAPTURE_REMOTE" "$spec" >/dev/null 2>&1 \
        && log "pushed $ref" \
        || { echo "$root|$spec" >> "$CAPTURE_HOME/pending"; log "push failed, queued $ref"; } ) \
      </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi
}

catch_up_transcripts() {
  # The transcript file is written asynchronously and a session can end abruptly,
  # so re-copy the transcripts of earlier sessions to pick up anything missed.
  local d sid path
  for d in "$CAPTURE_HOME"/state/*/; do
    sid="$(basename "$d")"
    [ "$sid" = "$SESSION_ID" ] && continue
    path="$(cat "$d/transcript_path" 2>/dev/null)"
    [ -n "$path" ] && [ -f "$path" ] || continue
    mkdir -p "$CAPTURE_OUTBOX/$sid"
    if ! cmp -s "$path" "$CAPTURE_OUTBOX/$sid/transcript.jsonl"; then
      cp -f "$path" "$CAPTURE_OUTBOX/$sid/transcript.jsonl" 2>/dev/null && log "caught up transcript for $sid"
    fi
  done
  return 0
}

flush_pending() {
  [ -s "$CAPTURE_HOME/pending" ] && [ -n "$CAPTURE_REMOTE" ] || return 0
  local tmp="$CAPTURE_HOME/pending.$$"
  mv "$CAPTURE_HOME/pending" "$tmp" 2>/dev/null || return 0
  while IFS='|' read -r root spec; do
    [ -d "$root" ] || continue
    git -C "$root" push -q "$CAPTURE_REMOTE" "$spec" >/dev/null 2>&1 \
      && log "retried and pushed $spec" \
      || echo "$root|$spec" >> "$CAPTURE_HOME/pending"
  done < "$tmp"
  rm -f "$tmp"
}

backup_branches() {
  # Mirror the latest tip of every local branch into a per-user namespace.
  in_repo || return 0
  local root; root="$(repo_root)"
  local ns="$REF_NS/users/$(user_slug)/heads"
  local b
  for b in $(git -C "$root" for-each-ref --format='%(refname:short)' refs/heads/); do
    git -C "$root" update-ref "$ns/$b" "refs/heads/$b" 2>/dev/null || continue
    push_ref "$ns/$b" force
  done
}

upload_transcript() {
  # POC: copy the transcript (and any sub-agent files beside it) to the outbox.
  [ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] || { log "no transcript file"; return 0; }
  local dest="$CAPTURE_OUTBOX/$SESSION_ID"
  mkdir -p "$dest"
  cp -f "$TRANSCRIPT" "$dest/transcript.jsonl" 2>/dev/null && log "transcript uploaded to $dest"
  # Sub-agent transcripts: Claude Code may keep them in a folder named after the
  # session next to the main transcript. Layout to be verified in Phase 1a.
  local side="${TRANSCRIPT%.jsonl}"
  [ -d "$side" ] && cp -rf "$side" "$dest/" 2>/dev/null && log "sub-agent files uploaded"
  return 0
}

snapshot() {
  # Snapshot the working tree as a hidden commit, without touching the developer's state.
  in_repo || { log "not a git repository: conversation-only session"; return 0; }
  local root; root="$(repo_root)"
  local head=""; head="$(git -C "$root" rev-parse -q --verify HEAD 2>/dev/null || true)"

  # 1. Private staging area: a temporary index file, never the developer's own.
  local idx; idx="$(mktemp "$CAPTURE_HOME/index.XXXXXX")"
  export GIT_INDEX_FILE="$idx"
  if [ -n "$head" ]; then git -C "$root" read-tree "$head"; else git -C "$root" read-tree --empty; fi
  # 2. Stage every file in the working tree into it; .gitignore is respected.
  git -C "$root" add -A . 2>/dev/null

  # 3. Secret scan of changed files; flagged files are reset to their HEAD version.
  local base="$head"; [ -z "$base" ] && base="$(git -C "$root" hash-object -t tree /dev/null)"
  local excluded="" f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if file_has_secret "$root/$f"; then
      git -C "$root" reset -q "${head:-HEAD}" -- "$f" 2>/dev/null || git -C "$root" rm -q --cached -- "$f" 2>/dev/null
      excluded="$excluded $f"
    fi
  done < <(git -C "$root" diff --cached --name-only --diff-filter=ACMR "$base" 2>/dev/null)

  local tree; tree="$(git -C "$root" write-tree)"
  unset GIT_INDEX_FILE; rm -f "$idx"

  # 4. Skip if nothing changed since this session's last snapshot.
  if [ -f "$STATE/last_tree" ] && [ "$(cat "$STATE/last_tree")" = "$tree" ]; then
    log "working tree unchanged since last snapshot; no snapshot"
    return 0
  fi
  if [ ! -f "$STATE/last_tree" ] && [ -n "$head" ] && [ "$(git -C "$root" rev-parse "$head^{tree}")" = "$tree" ]; then
    log "no uncommitted changes; no snapshot"
    return 0
  fi

  # 5. Unpushed local commits travel with the snapshot: scan them too.
  local nopush=""
  if [ -n "$head" ] && git -C "$root" log -p --format= "$head" --not --remotes 2>/dev/null \
       | grep -I -E -q -i -- "$SECRET_RE"; then
    nopush=1
    log "secret pattern found in unpushed commits; snapshot kept local only"
  fi

  # 6. Create the snapshot commit on top of the developer's current commit.
  local seq=$(( $(cat "$STATE/seq" 2>/dev/null || echo 0) + 1 ))
  local branch; branch="$(git -C "$root" symbolic-ref -q --short HEAD 2>/dev/null || echo detached)"
  local msg
  msg="claude-capture snapshot

session: $SESSION_ID
capture-id: $(cat "$STATE/capture_id" 2>/dev/null || echo unknown)
seq: $seq
event: ${EVENT:-$MODE}
branch: $branch
started-from: ${head:-none}
captured-at: $("$PY" -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))')
excluded:${excluded:- none}"
  local commit
  if [ -n "$head" ]; then
    commit="$(printf '%s\n' "$msg" | git -C "$root" commit-tree "$tree" -p "$head")"
  else
    commit="$(printf '%s\n' "$msg" | git -C "$root" commit-tree "$tree")"
  fi
  [ -n "$commit" ] || { log "commit-tree failed"; return 0; }

  # 7. Store it under a hidden ref (not a branch) and push it.
  local ref; ref="$REF_NS/sessions/$SESSION_ID/$(printf '%04d' "$seq")"
  git -C "$root" update-ref "$ref" "$commit"
  echo "$seq" > "$STATE/seq"; echo "$tree" > "$STATE/last_tree"
  log "snapshot $ref -> $commit${excluded:+ (excluded:$excluded)}"
  [ -z "$nopush" ] && push_ref "$ref"
  return 0
}

session_marker() {
  # SessionStart: add a marker to the conversation so the server-side transcript
  # carries the repository, branch and commit the session started from.
  local cid; cid="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
  echo "$cid" > "$STATE/capture_id"
  local repo="none" branch="none" head="none" email="unknown"
  if in_repo; then
    local root; root="$(repo_root)"
    repo="$(git -C "$root" remote get-url origin 2>/dev/null || echo "$root")"
    branch="$(git -C "$root" symbolic-ref -q --short HEAD 2>/dev/null || echo detached)"
    head="$(git -C "$root" rev-parse -q --verify HEAD 2>/dev/null || echo none)"
    email="$(git -C "$root" config user.email 2>/dev/null || echo unknown)"
  fi
  local text="Session capture reference for this conversation: capture_id=$cid repository=$repo branch=$branch commit=$head developer=$email"
  echo "$head" > "$STATE/start_commit"
  log "marker: $text"
  MARKER="$text" "$PY" -c '
import json, os
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": os.environ["MARKER"]}}))'
}

# ---------------------------------------------------------------- main

resolve_remote

case "$MODE" in
  session-start)
    session_marker
    snapshot >/dev/null 2>&1      # baseline: uncommitted work present before Claude starts
    catch_up_transcripts
    flush_pending
    backup_branches
    ;;
  snapshot)
    upload_transcript
    snapshot
    ;;
  session-end)
    upload_transcript
    snapshot
    backup_branches
    ;;
  *)
    log "unknown mode '$MODE'"
    ;;
esac
finish
~~~~

### A.2 `settings.windows.example.json`

~~~~json
{
  "env": {
    "CAPTURE_REMOTE_BASE": "C:/temp/capture-projects",
    "CAPTURE_SYNC": "1"
  },
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "\"C:\\Program Files\\Git\\bin\\bash.exe\" \"C:/Users/YOUR-NAME/.claude-capture/bin/capture.sh\" session-start", "timeout": 30 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "\"C:\\Program Files\\Git\\bin\\bash.exe\" \"C:/Users/YOUR-NAME/.claude-capture/bin/capture.sh\" snapshot", "timeout": 30 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "\"C:\\Program Files\\Git\\bin\\bash.exe\" \"C:/Users/YOUR-NAME/.claude-capture/bin/capture.sh\" session-end", "timeout": 30 } ] }
    ]
  }
}
~~~~

### A.3 `settings.example.json`

~~~~json
{
  "env": {
    "CAPTURE_REMOTE_BASE": "git@gitlab.example.com:claude-capture"
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.claude-capture/bin/capture.sh\" session-start",
            "timeout": 30,
            "statusMessage": "Claude capture: recording session marker"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.claude-capture/bin/capture.sh\" snapshot",
            "timeout": 30,
            "statusMessage": "Claude capture: snapshot"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"$HOME/.claude-capture/bin/capture.sh\" session-end",
            "timeout": 30,
            "statusMessage": "Claude capture: final snapshot"
          }
        ]
      }
    ]
  }
}
~~~~

### A.4 `install.sh`

~~~~bash
#!/usr/bin/env bash
# Install the claude-capture POC for the current user.
#   ./install.sh            copy the script to ~/.claude-capture/bin and print next steps
#   ./install.sh --apply    also merge the hooks into ~/.claude/settings.json (a backup is kept)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude-capture/bin"
mkdir -p "$DEST"
install -m 0755 "$HERE/capture.sh" "$DEST/capture.sh"
echo "Installed $DEST/capture.sh"

if [ "${1:-}" = "--apply" ]; then
  SETTINGS="$HOME/.claude/settings.json"
  mkdir -p "$HOME/.claude"
  [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak.$(date +%Y%m%d%H%M%S)" && echo "Backed up $SETTINGS"
  python3 - "$SETTINGS" "$HERE/settings.example.json" << 'EOF'
import json, os, sys
path, example = sys.argv[1], sys.argv[2]
cur = json.load(open(path)) if os.path.exists(path) else {}
new = json.load(open(example))
cur.setdefault("env", {})
for k, v in new.get("env", {}).items():
    cur["env"].setdefault(k, v)          # never overwrite an existing value
hooks = cur.setdefault("hooks", {})
for event, groups in new["hooks"].items():
    existing = hooks.setdefault(event, [])
    cmds = {h.get("command") for g in existing for h in g.get("hooks", [])}
    for g in groups:
        if not any(h.get("command") in cmds for h in g["hooks"]):
            existing.append(g)
json.dump(cur, open(path, "w"), indent=2)
print("Merged capture hooks into", path)
EOF
  echo "Edit CAPTURE_REMOTE_BASE in $SETTINGS to point at your capture group."
else
  echo "Next: merge settings.example.json into ~/.claude/settings.json (or run ./install.sh --apply),"
  echo "then set CAPTURE_REMOTE_BASE to your GitLab capture group."
fi
~~~~

### A.5 `kb_index.py`

~~~~python
#!/usr/bin/env python3
"""
kb_index.py - build the structured index (layer 2) from captured Claude sessions.

Reads:
  * transcripts in the outbox      (one folder per session, transcript.jsonl inside)
  * capture projects (Git)         (snapshot refs and branch backups)
Writes:
  * a SQLite database with sessions, messages, tool calls, snapshots and file changes,
    plus empty knowledge-base tables (layer 3) that curation will fill later.

Layer 2 is derived data: every `build` rebuilds it from the raw capture.
Layer 3 tables are never dropped; they refer to layer 2 by natural keys
(session ID, message UUID, tool-use ID, snapshot commit), so links survive rebuilds.

Usage:
  python kb_index.py build   [--db PATH] [--outbox DIR] [--capture PATH ...] [--state DIR]
  python kb_index.py report  [--db PATH]
  python kb_index.py export  SESSION_ID [--db PATH] [--out FILE]
  python kb_index.py query   "SQL" [--db PATH]

--capture accepts a capture project (bare or normal Git repository) or a folder that
contains capture projects (every *.git inside is loaded). Repeat it for several.
Requires Python 3.8+ and git on PATH. No other dependencies.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("CAPTURE_HOME", Path.home() / ".claude-capture"))
DEFAULT_DB = HOME / "capture-index.db"
DEFAULT_OUTBOX = Path(os.environ.get("CAPTURE_OUTBOX", HOME / "outbox"))
DEFAULT_STATE = HOME / "state"

MARKER_RE = re.compile(
    r"capture_id=(?P<capture_id>\S+)\s+repository=(?P<repository>\S+)\s+branch=(?P<branch>\S+)"
    r"\s+commit=(?P<commit>\S+)\s+developer=(?P<developer>[^\s\"\\]+)")

# ----------------------------------------------------------------------------- schema

LAYER2 = """
CREATE TABLE repository (
  name              TEXT PRIMARY KEY,
  capture_location  TEXT
);
CREATE TABLE developer (
  email             TEXT PRIMARY KEY
);
CREATE TABLE session (
  session_id        TEXT PRIMARY KEY,
  capture_id        TEXT,
  developer_email   TEXT,
  repository        TEXT,
  branch            TEXT,
  start_commit      TEXT,
  started_at        TEXT,
  ended_at          TEXT,
  title             TEXT,
  source            TEXT,          -- 'hook' now; 'compliance_api' in Phase 1b
  transcript_path   TEXT,
  message_count     INTEGER DEFAULT 0,
  tool_call_count   INTEGER DEFAULT 0,
  snapshot_count    INTEGER DEFAULT 0
);
CREATE TABLE message (
  uuid              TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  seq               INTEGER NOT NULL,
  parent_uuid       TEXT,
  role              TEXT,          -- user | assistant | system
  kind              TEXT,          -- prompt | response | tool_result | marker | other
  is_sidechain      INTEGER,       -- 1 = sub-agent activity
  timestamp         TEXT,
  text              TEXT
);
CREATE TABLE tool_call (
  tool_use_id       TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  message_uuid      TEXT,
  seq               INTEGER,
  name              TEXT,
  file_path         TEXT,          -- for Read/Write/Edit-type tools
  command           TEXT,          -- for Bash/PowerShell-type tools
  input_json        TEXT,
  result_text       TEXT,
  is_error          INTEGER,
  timestamp         TEXT
);
CREATE TABLE snapshot (
  commit_id         TEXT PRIMARY KEY,
  session_id        TEXT NOT NULL,
  seq               INTEGER,
  ref               TEXT,
  repository        TEXT,
  parent_commit     TEXT,
  captured_at       TEXT,
  event             TEXT,
  branch            TEXT,
  excluded          TEXT,
  is_baseline       INTEGER DEFAULT 0   -- 1 = starting state, uncommitted work present before the session
);
CREATE TABLE file_change (
  snapshot_commit   TEXT NOT NULL,
  compared_to       TEXT,          -- previous snapshot in the session, or the start commit
  path              TEXT NOT NULL,
  change_type       TEXT,          -- added | modified | deleted | other
  lines_added       INTEGER,
  lines_removed     INTEGER
);
CREATE TABLE branch_backup (
  repository        TEXT,
  developer         TEXT,
  branch            TEXT,
  commit_id         TEXT
);
CREATE INDEX ix_message_session   ON message(session_id, seq);
CREATE INDEX ix_tool_session      ON tool_call(session_id, seq);
CREATE INDEX ix_snapshot_session  ON snapshot(session_id, seq);
CREATE INDEX ix_change_path       ON file_change(path);

CREATE VIEW v_session_overview AS
SELECT s.session_id, s.title, s.developer_email, s.repository, s.branch,
       substr(s.start_commit, 1, 10) AS start_commit, s.started_at, s.ended_at,
       s.message_count, s.tool_call_count, s.snapshot_count,
       (SELECT group_concat(DISTINCT fc.path)
          FROM file_change fc JOIN snapshot sn ON sn.commit_id = fc.snapshot_commit
         WHERE sn.session_id = s.session_id AND sn.is_baseline = 0) AS files_touched
FROM session s;

CREATE VIEW v_timeline AS
SELECT session_id, timestamp AS at, 'message' AS item, role || ':' || kind AS detail,
       substr(replace(text, char(10), ' '), 1, 120) AS summary
  FROM message WHERE kind IN ('prompt', 'response')
UNION ALL
SELECT session_id, timestamp, 'tool', name,
       coalesce(file_path, substr(command, 1, 120), '')
  FROM tool_call
UNION ALL
SELECT session_id, captured_at, CASE is_baseline WHEN 1 THEN 'baseline' ELSE 'snapshot' END, 'seq ' || seq,
       substr(commit_id, 1, 10) || ' ' ||
       coalesce((SELECT group_concat(change_type || ' ' || path, ', ')
                   FROM file_change WHERE snapshot_commit = commit_id), '')
  FROM snapshot;

CREATE VIEW v_file_history AS
SELECT fc.path, fc.change_type, fc.lines_added, fc.lines_removed,
       sn.captured_at, sn.session_id, sn.seq, substr(sn.commit_id, 1, 10) AS snapshot,
       s.developer_email,
       (SELECT text FROM message m
         WHERE m.session_id = sn.session_id AND m.kind = 'prompt'
           AND m.timestamp <= sn.captured_at
         ORDER BY m.timestamp DESC LIMIT 1) AS preceding_prompt
FROM file_change fc
JOIN snapshot sn ON sn.commit_id = fc.snapshot_commit AND sn.is_baseline = 0
LEFT JOIN session s ON s.session_id = sn.session_id;
"""

LAYER2_OBJECTS = ["v_file_history", "v_timeline", "v_session_overview",
                  "branch_backup", "file_change", "snapshot", "tool_call",
                  "message", "session", "developer", "repository"]

LAYER3 = """
CREATE TABLE IF NOT EXISTS knowledge_entry (
  entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_type    TEXT CHECK (entry_type IN ('answer', 'decision', 'artifact')),
  title         TEXT NOT NULL,
  problem       TEXT,
  context       TEXT,
  answer        TEXT,
  caveats       TEXT,
  not_applicable_when TEXT,
  status        TEXT DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'superseded', 'rejected')),
  review_by     TEXT,
  created_at    TEXT DEFAULT (datetime('now')),
  embedding     BLOB              -- semantic-search vector (pgvector in PostgreSQL)
);
CREATE TABLE IF NOT EXISTS evidence_link (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  evidence_type TEXT CHECK (evidence_type IN ('session', 'message', 'tool_call', 'snapshot')),
  evidence_key  TEXT              -- session_id, message uuid, tool_use_id or commit id
);
CREATE TABLE IF NOT EXISTS code_reference (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  repository    TEXT,
  start_commit  TEXT,
  final_snapshot TEXT,
  diff_text     TEXT
);
CREATE TABLE IF NOT EXISTS decision_link (
  from_entry    INTEGER REFERENCES knowledge_entry(entry_id),
  relation      TEXT CHECK (relation IN ('supersedes', 'depends_on', 'affects')),
  to_entry      INTEGER,
  to_text       TEXT              -- for assumptions or systems that are not entries
);
CREATE TABLE IF NOT EXISTS review (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  reviewer      TEXT,
  role          TEXT CHECK (role IN ('author', 'expert')),
  outcome       TEXT CHECK (outcome IN ('confirmed', 'approved', 'changes_requested', 'declined')),
  comment       TEXT,
  reviewed_at   TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS feedback (
  entry_id      INTEGER REFERENCES knowledge_entry(entry_id),
  user_email    TEXT,
  helpful       INTEGER,
  comment       TEXT,
  given_at      TEXT DEFAULT (datetime('now'))
);
"""

# ----------------------------------------------------------------------------- helpers

def git(repo, *args):
    """Run git in a repository and return stdout ('' on failure)."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        return out.stdout if out.returncode == 0 else ""
    except FileNotFoundError:
        sys.exit("git was not found on PATH")


def content_text(content):
    """Flatten Claude message content (string or list of blocks) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(content_text(block.get("content")))
    return "\n".join(p for p in parts if p)


def find_marker(raw_line):
    m = MARKER_RE.search(raw_line)
    return m.groupdict() if m else None


def capture_projects(paths):
    """Expand --capture arguments into a list of Git repositories."""
    repos = []
    for p in paths:
        p = Path(p)
        if (p / "HEAD").exists() or (p / ".git").exists():
            repos.append(p)
        elif p.is_dir():
            repos.extend(sorted(q for q in p.iterdir() if q.is_dir() and (q / "HEAD").exists()))
    return repos


def repo_name(repo):
    name = Path(repo).name
    for suffix in (".git", "-capture"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name

# ----------------------------------------------------------------------------- loaders

def load_transcript(db, session_id, path, state_dir):
    """Parse one Claude Code transcript (JSON lines) into session/message/tool_call rows."""
    session = {"session_id": session_id, "source": "hook", "transcript_path": str(path)}
    seq = 0
    pending_tools = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            marker = find_marker(raw)
            if marker and not session.get("capture_id"):
                repo_from_marker = marker["repository"].rstrip("/").split("/")[-1].split(":")[-1]
                session.update(capture_id=marker["capture_id"], repository=repo_name(repo_from_marker),
                               branch=marker["branch"], start_commit=marker["commit"],
                               developer_email=marker["developer"])
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(line, dict):
                continue
            if line.get("type") == "summary" and line.get("summary"):
                session.setdefault("title", line["summary"])
                continue
            ts = line.get("timestamp")
            if ts:
                session["started_at"] = min(session.get("started_at") or ts, ts)
                session["ended_at"] = max(session.get("ended_at") or ts, ts)
            if line.get("gitBranch") and not session.get("branch"):
                session["branch"] = line["gitBranch"]
            msg = line.get("message")
            if not isinstance(msg, dict):
                if marker:
                    seq += 1
                    db.execute("INSERT OR REPLACE INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                               (line.get("uuid") or f"{session_id}:{seq}", session_id, seq,
                                line.get("parentUuid"), "system", "marker", 0, ts, raw[:2000]))
                continue
            seq += 1
            role = msg.get("role") or line.get("type")
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            is_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
            if marker:
                kind = "marker"
            elif role == "user" and is_tool_result:
                kind = "tool_result"
            elif role == "user" and not line.get("isMeta"):
                kind = "prompt"
            elif role == "assistant":
                kind = "response"
            else:
                kind = "other"
            uuid = line.get("uuid") or f"{session_id}:{seq}"
            db.execute("INSERT OR REPLACE INTO message VALUES (?,?,?,?,?,?,?,?,?)",
                       (uuid, session_id, seq, line.get("parentUuid"), role, kind,
                        1 if line.get("isSidechain") else 0, ts, content_text(content)))
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    file_path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                    command = inp.get("command")
                    tid = b.get("id") or f"{uuid}:tool"
                    db.execute("INSERT OR REPLACE INTO tool_call VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (tid, session_id, uuid, seq, b.get("name"), file_path, command,
                                json.dumps(inp, ensure_ascii=False)[:20000], None, None, ts))
                    pending_tools[tid] = True
                elif b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        db.execute("UPDATE tool_call SET result_text = ?, is_error = ? WHERE tool_use_id = ?",
                                   (content_text(b.get("content"))[:20000],
                                    1 if b.get("is_error") else 0, tid))
    # Fall back to the hook's own state if the marker was not found in the transcript
    st = Path(state_dir) / session_id
    if not session.get("capture_id") and (st / "capture_id").exists():
        session["capture_id"] = (st / "capture_id").read_text().strip()
    if not session.get("start_commit") and (st / "start_commit").exists():
        session["start_commit"] = (st / "start_commit").read_text().strip()
    upsert_session(db, session)


def upsert_session(db, s):
    cols = ["session_id", "capture_id", "developer_email", "repository", "branch", "start_commit",
            "started_at", "ended_at", "title", "source", "transcript_path"]
    existing = db.execute("SELECT * FROM session WHERE session_id = ?", (s["session_id"],)).fetchone()
    if existing:
        sets = ", ".join(f"{c} = coalesce({c}, ?)" for c in cols[1:])
        db.execute(f"UPDATE session SET {sets} WHERE session_id = ?",
                   [s.get(c) for c in cols[1:]] + [s["session_id"]])
    else:
        db.execute(f"INSERT INTO session ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   [s.get(c) for c in cols])


def load_capture_project(db, repo):
    """Load snapshot refs, their file changes and branch backups from one Git repository."""
    name = repo_name(repo)
    db.execute("INSERT OR IGNORE INTO repository VALUES (?, ?)", (name, str(repo)))
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/claude-capture")
    snaps = []
    for line in refs.splitlines():
        try:
            ref, commit = line.split(" ", 1)
        except ValueError:
            continue
        parts = ref.split("/")
        if len(parts) >= 5 and parts[2] == "sessions":
            snaps.append((parts[3], int(parts[4]) if parts[4].isdigit() else 0, ref, commit))
        elif len(parts) >= 6 and parts[2] == "users" and parts[4] == "heads":
            row = (name, parts[3], "/".join(parts[5:]), commit)
            if not db.execute("SELECT 1 FROM branch_backup WHERE repository=? AND developer=? AND branch=? AND commit_id=?",
                              row).fetchone():
                db.execute("INSERT INTO branch_backup VALUES (?,?,?,?)", row)
    snaps.sort()
    previous = {}
    for session_id, seq, ref, commit in snaps:
        info = git(repo, "log", "-1", "--format=%P%x00%cI%x00%B", commit)
        parent, committed_at, body = (info.split("\x00", 2) + ["", "", ""])[:3]
        parent = parent.split()[0] if parent.strip() else None
        fields = dict(re.findall(r"^([a-z-]+):\s*(.*)$", body, flags=re.M))
        captured_at = fields.get("captured-at") or committed_at.strip()
        event = fields.get("event") or ""
        baseline = 1 if event.replace("-", "").lower() == "sessionstart" else 0
        db.execute("INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (commit, session_id, seq, ref, name, parent, captured_at,
                    event, fields.get("branch"), fields.get("excluded"), baseline))
        db.execute("DELETE FROM file_change WHERE snapshot_commit = ?", (commit,))   # loaded from two places
        # Compare with the previous snapshot of this session, or with the start commit
        base = previous.get(session_id) or parent
        previous[session_id] = commit
        if base:
            status = git(repo, "diff", "--name-status", "--no-renames", base, commit)
            numstat = git(repo, "diff", "--numstat", "--no-renames", base, commit)
        else:
            empty = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # Git's empty tree
            status = git(repo, "diff", "--name-status", "--no-renames", empty, commit)
            numstat = git(repo, "diff", "--numstat", "--no-renames", empty, commit)
        counts = {}
        for nl in numstat.splitlines():
            bits = nl.split("\t")
            if len(bits) == 3:
                a, r, p = bits
                counts[p] = (int(a) if a.isdigit() else None, int(r) if r.isdigit() else None)
        kinds = {"A": "added", "M": "modified", "D": "deleted"}
        for sl in status.splitlines():
            bits = sl.split("\t")
            if len(bits) >= 2:
                code, p = bits[0], bits[-1]
                a, r = counts.get(p, (None, None))
                db.execute("INSERT INTO file_change VALUES (?,?,?,?,?,?)",
                           (commit, base, p, kinds.get(code[:1], "other"), a, r))
        # Make sure every snapshot has a session row, even without a transcript
        upsert_session(db, {"session_id": session_id, "repository": name,
                            "branch": fields.get("branch"),
                            "start_commit": fields.get("started-from") if fields.get("started-from") != "none" else None,
                            "capture_id": fields.get("capture-id") if fields.get("capture-id") != "unknown" else None,
                            "source": "hook"})


def build(args):
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    for obj in LAYER2_OBJECTS:                       # rebuild derived layer 2 only
        kind = "VIEW" if obj.startswith("v_") else "TABLE"
        db.execute(f"DROP {kind} IF EXISTS {obj}")
    db.executescript(LAYER2)
    db.executescript(LAYER3)                         # layer 3 is created once, never dropped

    outbox = Path(args.outbox)
    n_transcripts = 0
    if outbox.is_dir():
        for folder in sorted(p for p in outbox.iterdir() if p.is_dir()):
            t = folder / "transcript.jsonl"
            if t.exists():
                load_transcript(db, folder.name, t, args.state)
                n_transcripts += 1
    repos = capture_projects(args.capture)
    for repo in repos:
        load_capture_project(db, repo)

    db.execute("""INSERT OR IGNORE INTO developer
                  SELECT DISTINCT developer_email FROM session WHERE developer_email IS NOT NULL""")
    db.execute("""UPDATE session SET
        message_count  = (SELECT count(*) FROM message   WHERE message.session_id   = session.session_id AND kind IN ('prompt','response')),
        tool_call_count= (SELECT count(*) FROM tool_call WHERE tool_call.session_id = session.session_id),
        snapshot_count = (SELECT count(*) FROM snapshot  WHERE snapshot.session_id  = session.session_id AND is_baseline = 0)""")
    db.commit()
    totals = {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("session", "message", "tool_call", "snapshot", "file_change", "branch_backup")}
    print(f"Index built: {db_path}")
    print(f"  transcripts read: {n_transcripts}   capture projects read: {len(repos)}")
    for t, n in totals.items():
        print(f"  {t:<14}{n}")
    db.close()

# ----------------------------------------------------------------------------- output

def print_rows(cur, max_width=60):
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    cells = [[("" if v is None else str(v)).replace("\n", " ")[:max_width] for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))
    if not rows:
        print("(no rows)")


def report(args):
    db = sqlite3.connect(args.db)
    print("\nSESSIONS")
    print_rows(db.execute("""SELECT substr(session_id,1,8) AS session, developer_email, repository,
                             start_commit, message_count AS msgs, tool_call_count AS tools,
                             snapshot_count AS snaps, files_touched
                             FROM v_session_overview ORDER BY started_at"""))
    print("\nFILES CHANGED BY CLAUDE, WITH THE PROMPT THAT LED TO EACH CHANGE")
    print_rows(db.execute("""SELECT path, change_type, substr(session_id,1,8) AS session, seq,
                             preceding_prompt FROM v_file_history ORDER BY captured_at"""))
    print("\nTOOLS USED")
    print_rows(db.execute("""SELECT name, count(*) AS uses, sum(coalesce(is_error,0)) AS errors
                             FROM tool_call GROUP BY name ORDER BY uses DESC"""))
    print("\nSESSIONS WITH NO CODE CHANGE (often pure Q&A: good knowledge candidates)")
    print_rows(db.execute("""SELECT substr(session_id,1,8) AS session, developer_email, message_count AS msgs,
                             (SELECT text FROM message m WHERE m.session_id = s.session_id
                               AND m.kind = 'prompt' ORDER BY seq LIMIT 1) AS first_prompt
                             FROM session s
                             WHERE message_count > 0 AND NOT EXISTS
                               (SELECT 1 FROM snapshot sn JOIN file_change fc ON fc.snapshot_commit = sn.commit_id
                                 WHERE sn.session_id = s.session_id AND sn.is_baseline = 0)"""))
    db.close()


def export(args):
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    s = db.execute("SELECT * FROM session WHERE session_id LIKE ?", (args.session + "%",)).fetchone()
    if not s:
        sys.exit(f"No session starting with {args.session}")
    sid = s["session_id"]
    out = [f"# Session {sid}", "",
           f"- **Developer:** {s['developer_email'] or 'unknown'}",
           f"- **Repository:** {s['repository'] or 'unknown'} (branch {s['branch'] or '?'})",
           f"- **Started from commit:** {s['start_commit'] or 'unknown'}",
           f"- **Time:** {s['started_at'] or '?'} to {s['ended_at'] or '?'}",
           f"- **Capture ID:** {s['capture_id'] or 'unknown'}", ""]
    items = []
    for m in db.execute("SELECT * FROM message WHERE session_id = ? AND kind IN ('prompt','response')", (sid,)):
        if m["text"].strip():
            items.append((m["timestamp"] or "", m["seq"], "msg", m))
    for t in db.execute("SELECT * FROM tool_call WHERE session_id = ?", (sid,)):
        items.append((t["timestamp"] or "", t["seq"], "tool", t))
    for sn in db.execute("SELECT * FROM snapshot WHERE session_id = ?", (sid,)):
        items.append((sn["captured_at"] or "", 10 ** 9, "snap", sn))
    for _, _, kind, r in sorted(items, key=lambda x: (x[0], x[1])):
        if kind == "msg":
            who = "Developer" if r["role"] == "user" else "Claude"
            sub = " (sub-agent)" if r["is_sidechain"] else ""
            out += [f"## {who}{sub}", "", r["text"].strip(), ""]
        elif kind == "tool":
            target = r["file_path"] or (r["command"] or "")[:200]
            status = "failed" if r["is_error"] else "ok"
            out += [f"> Tool **{r['name']}** `{target}` ({status})", ""]
        else:
            changes = db.execute("SELECT change_type, path, lines_added, lines_removed FROM file_change "
                                 "WHERE snapshot_commit = ?", (r["commit_id"],)).fetchall()
            if r["is_baseline"]:
                out += [f"### Starting state ({r['commit_id'][:10]}): uncommitted work already present when the session began", ""]
            else:
                out += [f"### Snapshot {r['seq']} ({r['commit_id'][:10]}, {r['captured_at']})", ""]
            out += [f"- {c['change_type']} `{c['path']}` (+{c['lines_added'] or 0} / -{c['lines_removed'] or 0})"
                    for c in changes] or ["- no file changes"]
            if r["excluded"] and r["excluded"].strip() != "none":
                out.append(f"- excluded for secrets: {r['excluded'].strip()}")
            out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(text)
    db.close()


def query(args):
    db = sqlite3.connect(args.db)
    print_rows(db.execute(args.sql), max_width=100)
    db.close()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Build and explore the structured capture index.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="rebuild the index from the outbox and capture projects")
    b.add_argument("--outbox", default=str(DEFAULT_OUTBOX))
    b.add_argument("--capture", action="append", default=[],
                   help="capture project, or folder of capture projects (repeatable)")
    b.add_argument("--state", default=str(DEFAULT_STATE))
    r = sub.add_parser("report", help="print an overview with sample queries")
    e = sub.add_parser("export", help="write one session as readable Markdown")
    e.add_argument("session", help="session ID (the first few characters are enough)")
    e.add_argument("--out")
    q = sub.add_parser("query", help="run your own SQL against the index")
    q.add_argument("sql")
    for p in (b, r, e, q):
        p.add_argument("--db", default=str(DEFAULT_DB))
    args = ap.parse_args()
    {"build": build, "report": report, "export": export, "query": query}[args.cmd](args)


if __name__ == "__main__":
    main()
~~~~

### A.6 `test_poc.sh`

~~~~bash
#!/usr/bin/env bash
# End-to-end test of capture.sh. Simulates the JSON that Claude Code sends to hooks.
# Creates everything in a temporary directory; touches nothing else.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CAP="$HERE/capture.sh"
T="$(mktemp -d)"
export CAPTURE_HOME="$T/capture-home" CAPTURE_SYNC=1
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

hook() { # hook <mode> <session> <cwd>
  printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s","hook_event_name":"%s"}' \
    "$2" "$T/transcript-$2.jsonl" "$3" "$1" | "$CAP" "$1"
}

echo "Setting up a developer repo, its main remote and a capture project in $T"
git init -q --bare "$T/main.git"
git init -q --bare "$T/capture.git"
git init -q -b main "$T/repo"; cd "$T/repo"
git config user.email dev@example.com; git config user.name Dev
printf '.env\n*.key\n' > .gitignore
echo 'print("v1")' > app.py
echo 'staged base' > staged.txt
git add -A && git commit -qm "initial" && git remote add origin "$T/main.git" && git push -q origin main
START="$(git rev-parse HEAD)"
export CAPTURE_REMOTE="$T/capture.git"
SID="sess-1111"
echo '{"type":"user","message":"fix the bug"}' > "$T/transcript-$SID.jsonl"

echo; echo "1. Session start"
OUT="$(hook session-start "$SID" "$T/repo")"
check "marker returned as additional context" "echo '$OUT' | grep -q 'capture_id=.*commit=$START'"
check "branch backup pushed to capture project" "git -C $T/capture.git rev-parse -q --verify refs/claude-capture/users/dev_example.com/heads/main >/dev/null"

echo; echo "2. Claude edits files; developer has staged something of their own"
echo 'staged change by developer' > staged.txt && git add staged.txt
echo 'print("v2 - edited by Claude")' > app.py
echo '# notes' > notes.md
echo 'AWS_SECRET=abc' > .env
echo 'API_KEY = "sk_live_0123456789abcdef"' > config.py
STAGED_BEFORE="$(git diff --cached --name-only)"
hook snapshot "$SID" "$T/repo"
S1="refs/claude-capture/sessions/$SID/0001"
check "snapshot 1 pushed to capture project" "git -C $T/capture.git rev-parse -q --verify $S1 >/dev/null"
check "developer HEAD unchanged" "[ \"\$(git rev-parse HEAD)\" = '$START' ]"
check "developer staged changes untouched" "[ \"\$(git diff --cached --name-only)\" = '$STAGED_BEFORE' ]"
check "developer working files untouched" "grep -q v2 app.py && [ -f config.py ]"
check "no new branch created" "[ \"\$(git branch --format='%(refname:short)')\" = 'main' ]"
check "snapshot contains uncommitted Claude edit" "git -C $T/capture.git show $S1:app.py | grep -q v2"
check "snapshot contains new untracked file" "git -C $T/capture.git show $S1:notes.md >/dev/null 2>&1"
check "ignored .env excluded" "! git -C $T/capture.git show $S1:.env >/dev/null 2>&1"
check "file with secret excluded" "! git -C $T/capture.git show $S1:config.py >/dev/null 2>&1"
check "exclusion recorded in snapshot message" "git -C $T/capture.git log -1 --format=%B $S1 | grep -q 'excluded: config.py'"
check "snapshot parent is developer's commit" "[ \"\$(git -C $T/capture.git rev-parse $S1^)\" = '$START' ]"
check "transcript uploaded" "[ -f $CAPTURE_HOME/outbox/$SID/transcript.jsonl ]"

echo; echo "3. Claude responds without changing files"
hook snapshot "$SID" "$T/repo"
check "no duplicate snapshot" "! git -C $T/capture.git rev-parse -q --verify refs/claude-capture/sessions/$SID/0002 >/dev/null"

echo; echo "4. Claude edits again"
echo 'print("v3")' > app.py
hook snapshot "$SID" "$T/repo"
S2="refs/claude-capture/sessions/$SID/0002"
check "snapshot 2 pushed" "git -C $T/capture.git rev-parse -q --verify $S2 >/dev/null"
check "diff between snapshots shows only the new edit" "[ \"\$(git -C $T/capture.git diff --name-only $S1 $S2)\" = 'app.py' ]"

echo; echo "5. Developer commits locally but does not push"
rm -f config.py; git add -A && git commit -qm "local work, not pushed"
LOCAL="$(git rev-parse HEAD)"
echo 'print("v4")' > app.py
hook snapshot "$SID" "$T/repo"
S3="refs/claude-capture/sessions/$SID/0003"
check "snapshot 3 builds on the unpushed commit" "[ \"\$(git -C $T/capture.git rev-parse $S3^)\" = '$LOCAL' ]"
check "unpushed commit now preserved in capture project" "git -C $T/capture.git cat-file -e $LOCAL"
check "unpushed commit still not in main repository" "! git -C $T/main.git cat-file -e $LOCAL 2>/dev/null"

echo; echo "6. Capture project unreachable, then back"
echo 'print("v5")' > app.py
CAPTURE_REMOTE="$T/does-not-exist.git" hook snapshot "$SID" "$T/repo"
S4="refs/claude-capture/sessions/$SID/0004"
check "failed push queued, session not blocked" "grep -q '$S4' $CAPTURE_HOME/pending"
hook session-start "sess-2222" "$T/repo" >/dev/null
check "queued snapshot delivered at next session start" "git -C $T/capture.git rev-parse -q --verify $S4 >/dev/null"

echo; echo "6b. Transcript written late (after the session's last hook)"
echo '{"type":"assistant","message":"late line"}' >> "$T/transcript-$SID.jsonl"
hook session-start "sess-4444" "$T/repo" >/dev/null
check "late transcript lines caught up at next session start" "grep -q 'late line' $CAPTURE_HOME/outbox/$SID/transcript.jsonl"

echo; echo "6c. Capture project derived from CAPTURE_REMOTE_BASE"
git init -q --bare "$T/main-capture.git"
echo 'print("base")' > app.py
CAPTURE_REMOTE= CAPTURE_REMOTE_BASE="$T" hook snapshot "sess-5555" "$T/repo"
check "snapshot pushed to <base>/<repo>-capture.git" "git -C $T/main-capture.git rev-parse -q --verify refs/claude-capture/sessions/sess-5555/0001 >/dev/null"

echo; echo "7. Session end and a session outside any repository"
echo 'print("v6")' > app.py
hook session-end "$SID" "$T/repo"
check "final snapshot pushed" "git -C $T/capture.git rev-parse -q --verify refs/claude-capture/sessions/$SID/0005 >/dev/null"
mkdir -p "$T/not-a-repo"; echo '{}' > "$T/transcript-sess-3333.jsonl"
hook snapshot "sess-3333" "$T/not-a-repo"; RC=$?
check "non-repository session exits cleanly" "[ $RC -eq 0 ]"
check "non-repository session transcript still uploaded" "[ -f $CAPTURE_HOME/outbox/sess-3333/transcript.jsonl ]"

echo; echo "Capture project contents:"
git -C "$T/capture.git" for-each-ref --format='  %(refname)' refs/claude-capture
echo; echo "Result: $PASS passed, $FAIL failed"
rm -rf "$T"
[ "$FAIL" -eq 0 ]
~~~~

### A.7 `test_index.sh`

~~~~bash
#!/usr/bin/env bash
# End-to-end test of kb_index.py: real snapshots from capture.sh plus transcripts in
# Claude Code's JSON-lines format, then build, report, export and layer-3 persistence.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
CAP="$HERE/capture.sh"; IDX="python3 $HERE/kb_index.py"
T="$(mktemp -d)"
export CAPTURE_HOME="$T/capture-home" CAPTURE_SYNC=1 CAPTURE_REMOTE_BASE="$T/group"
DB="$T/index.db"
PASS=0; FAIL=0
check(){ if eval "$2"; then echo "  PASS  $1"; PASS=$((PASS+1)); else echo "  FAIL  $1"; FAIL=$((FAIL+1)); fi; }
sql(){ python3 -c "import sqlite3,sys; print(sqlite3.connect('$DB').execute(sys.argv[1]).fetchone()[0])" "$1"; }
hook(){ printf '{"session_id":"%s","transcript_path":"%s","cwd":"%s","hook_event_name":"%s"}' \
          "$2" "$T/t-$2.jsonl" "$3" "$1" | "$CAP" "$1"; }
now(){ python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]+"Z")'; }
line(){ echo "$1" >> "$T/t-$2.jsonl"; sleep 1; }

mkdir -p "$T/group"; git init -q --bare "$T/group/billing-app-capture.git"
git init -q --bare "$T/billing-app.git"
git init -q -b main "$T/billing-app"; cd "$T/billing-app"
git config user.email dev@example.com; git config user.name Dev
echo "old notes" > notes.txt; git add -A; git commit -qm initial
git remote add origin "$T/billing-app.git"; git push -q origin main

echo "Session A: code changes, a deletion and a sub-agent"
A="aaaa1111-0000-0000-0000-000000000001"; : > "$T/t-$A.jsonl"
MARK="$(hook session-start "$A" "$T/billing-app" | python3 -c 'import json,sys;print(json.load(sys.stdin)["hookSpecificOutput"]["additionalContext"])')"
line "$(python3 -c 'import json,sys;print(json.dumps({"type":"attachment","sessionId":sys.argv[1],"timestamp":sys.argv[3],"attachment":{"type":"hook_additional_context","content":[sys.argv[2]]}}))' "$A" "$MARK" "$(now)")" "$A"
line '{"type":"user","uuid":"a-u1","parentUuid":null,"sessionId":"'$A'","timestamp":"'$(now)'","gitBranch":"main","message":{"role":"user","content":"Add a function to compute invoice totals with tax"}}' "$A"
line '{"type":"assistant","uuid":"a-a1","parentUuid":"a-u1","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"I will create invoice.py."},{"type":"tool_use","id":"toolu_write1","name":"Write","input":{"file_path":"'$T'/billing-app/invoice.py","content":"def total(x, tax): return x * (1 + tax)\n"}}]}}' "$A"
printf 'def total(x, tax):\n    return x * (1 + tax)\n' > invoice.py
line '{"type":"user","uuid":"a-u2","parentUuid":"a-a1","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_write1","content":"File created successfully"}]}}' "$A"
line '{"type":"assistant","uuid":"a-a2","parentUuid":"a-u2","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Created invoice.py with a total() function."}]}}' "$A"
hook snapshot "$A" "$T/billing-app"
line '{"type":"user","uuid":"a-u3","parentUuid":"a-a2","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"user","content":"Remove the old notes file"}}' "$A"
line '{"type":"assistant","uuid":"a-a3","parentUuid":"a-u3","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bash1","name":"Bash","input":{"command":"rm notes.txt"}}]}}' "$A"
rm notes.txt
line '{"type":"user","uuid":"a-u4","parentUuid":"a-a3","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bash1","content":"","is_error":false}]}}' "$A"
line '{"type":"assistant","uuid":"a-s1","parentUuid":"a-u4","isSidechain":true,"sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Sub-agent: no other references to notes.txt found."}]}}' "$A"
line '{"type":"assistant","uuid":"a-a4","parentUuid":"a-s1","sessionId":"'$A'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Deleted notes.txt."}]}}' "$A"
line '{"type":"summary","summary":"Invoice totals and cleanup","leafUuid":"a-a4"}' "$A"
hook session-end "$A" "$T/billing-app"

echo "Session B: a question with no code change"
B="bbbb2222-0000-0000-0000-000000000002"; : > "$T/t-$B.jsonl"
hook session-start "$B" "$T/billing-app" >/dev/null
line '{"type":"user","uuid":"b-u1","parentUuid":null,"sessionId":"'$B'","timestamp":"'$(now)'","message":{"role":"user","content":"How do I configure the proxy for the billing API?"}}' "$B"
line '{"type":"assistant","uuid":"b-a1","parentUuid":"b-u1","sessionId":"'$B'","timestamp":"'$(now)'","message":{"role":"assistant","content":[{"type":"text","text":"Set HTTPS_PROXY before starting the service."}]}}' "$B"
hook session-end "$B" "$T/billing-app"

echo; echo "Build"
$IDX build --db "$DB" --capture "$T/group"
check "two sessions indexed" "[ \"\$(sql 'select count(*) from session')\" = 2 ]"
check "marker linked: developer, repository, start commit" "[ \"\$(sql \"select developer_email||'|'||repository||'|'||start_commit from session where session_id='$A'\")\" = \"dev@example.com|billing-app|\$(git rev-parse HEAD)\" ]"
check "session title from summary" "[ \"\$(sql \"select title from session where session_id='$A'\")\" = 'Invoice totals and cleanup' ]"
check "prompts and responses classified (2 prompts, 5 responses)" "[ \"\$(sql \"select count(*) from message where session_id='$A' and kind in ('prompt','response')\")\" = 7 ]"
check "sub-agent message flagged" "[ \"\$(sql \"select is_sidechain from message where uuid='a-s1'\")\" = 1 ]"
check "Write tool call with file path and result" "[ \"\$(sql \"select name||'|'||(file_path like '%invoice.py')||'|'||result_text from tool_call where tool_use_id='toolu_write1'\")\" = 'Write|1|File created successfully' ]"
check "Bash tool call with command" "[ \"\$(sql \"select command from tool_call where tool_use_id='toolu_bash1'\")\" = 'rm notes.txt' ]"
check "two snapshots for session A" "[ \"\$(sql \"select count(*) from snapshot where session_id='$A'\")\" = 2 ]"
check "invoice.py recorded as added (+2 lines)" "[ \"\$(sql \"select change_type||lines_added from file_change where path='invoice.py'\")\" = added2 ]"
check "notes.txt recorded as deleted" "[ \"\$(sql \"select change_type from file_change where path='notes.txt'\")\" = deleted ]"
check "change linked to the prompt that caused it" "[ \"\$(sql \"select preceding_prompt from v_file_history where path='invoice.py'\")\" = 'Add a function to compute invoice totals with tax' ]"
check "deletion linked to its prompt" "[ \"\$(sql \"select preceding_prompt from v_file_history where path='notes.txt'\")\" = 'Remove the old notes file' ]"
check "Q&A session credited with no changes" "[ \"\$(sql \"select snapshot_count from session where session_id='$B'\")\" = 0 ]"
check "Q&A session keeps a baseline of the pre-existing uncommitted work" "[ \"\$(sql \"select count(*) from snapshot where session_id='$B' and is_baseline=1\")\" = 1 ]"
check "Q&A session listed as a knowledge candidate" "python3 $HERE/kb_index.py report --db $DB | grep -A3 'NO CODE CHANGE' | grep -q 'proxy'"
check "branch backup indexed" "[ \"\$(sql 'select count(*) from branch_backup')\" -ge 1 ]"

echo; echo "Report"
$IDX report --db "$DB"
echo; echo "Export"
$IDX export aaaa1111 --db "$DB" --out "$T/session.md"
cat "$T/session.md"
check "export contains conversation, tool calls and snapshots" "grep -q 'Add a function' $T/session.md && grep -q 'Tool \*\*Write\*\*' $T/session.md && grep -q 'deleted .notes.txt' $T/session.md"

echo; echo "Same snapshots loaded from two places"
$IDX build --db "$DB" --capture "$T/group" --capture "$T/billing-app" >/dev/null
check "no duplicate file changes" "[ \"\$(sql \"select count(*) from file_change where path='invoice.py' and snapshot_commit in (select commit_id from snapshot where session_id='$A')\")\" = 1 ]"

echo; echo "Layer 3 survives a rebuild"
python3 -c "import sqlite3; c=sqlite3.connect('$DB'); c.execute(\"insert into knowledge_entry(entry_type,title) values('answer','Configure the billing API proxy')\"); c.execute(\"insert into evidence_link values(1,'session','$B')\"); c.commit()"
$IDX build --db "$DB" --capture "$T/group" >/dev/null
check "knowledge entry and its evidence link kept" "[ \"\$(sql \"select k.title from knowledge_entry k join evidence_link e on e.entry_id=k.entry_id join session s on s.session_id=e.evidence_key\")\" = 'Configure the billing API proxy' ]"

echo; echo "Result: $PASS passed, $FAIL failed"
rm -rf "$T"; [ "$FAIL" -eq 0 ]
~~~~

### A.8 `README.md`

~~~~markdown
# claude-capture POC

A local Claude Code hook script that captures what a developer and Claude did in a session,
without the developer committing anything:

- **Session start**: records a capture marker in the conversation (repository, branch,
  starting commit, developer) and backs up every local branch.
- **After each Claude response**: copies the session transcript and, if files changed,
  snapshots the working tree, including uncommitted edits and new files.
- **Session end**: final transcript copy, final snapshot, branch backup.

Snapshots are stored as hidden Git refs (`refs/claude-capture/...`) and pushed to a separate
capture project. The developer's branch, commits, staged changes and files are never touched.

## Files

| File | Purpose |
| --- | --- |
| `capture.sh` | The hook script (bash; needs `git` and `python3`; uses `gitleaks` if installed) |
| `settings.example.json` | Claude Code settings that wire the script to the three hook events |
| `install.sh` | Copies the script to `~/.claude-capture/bin`; `--apply` merges the hooks into `~/.claude/settings.json` (keeps a backup, never overwrites existing values) |
| `test_poc.sh` | End-to-end test that simulates Claude Code's hook calls in a throwaway repo |

## Try it

1. Run the automated test (no Claude Code needed). It builds a temporary repo, a "main"
   remote and a capture project, then simulates a session:

   ```
   ./test_poc.sh
   ```

   It checks that uncommitted edits and new files are captured, ignored and secret-bearing
   files are excluded, the developer's HEAD and staged changes are untouched, unpushed local
   commits are preserved, failed pushes are retried, and late transcript lines are caught up.

2. Try it with Claude Code on a scratch repository:

   ```
   ./install.sh --apply
   ```

   Then set `CAPTURE_REMOTE_BASE` in `~/.claude/settings.json` to a GitLab group where a
   project named `<repository>-capture` exists, or set `CAPTURE_REMOTE` to one capture
   project's URL. With neither set, snapshots stay local only.

   Start `claude` in the scratch repository, ask it to change a few files, then check:

   ```
   git for-each-ref refs/claude-capture                    # hidden snapshot refs
   git log -1 --format=%B refs/claude-capture/sessions/<session>/0001
   git diff <starting commit> refs/claude-capture/sessions/<session>/0002
   ls ~/.claude-capture/outbox/                             # captured transcripts
   cat ~/.claude-capture/capture.log
   ```

   Run `/hooks` inside Claude Code to confirm the three hooks are registered.

## Structured index (`kb_index.py`)

Turns the raw capture into a queryable SQLite database: sessions, messages, tool calls,
snapshots and file changes (layer 2), plus empty knowledge-base tables (layer 3) for curation.
Pure Python standard library plus `git`.

```
python kb_index.py build --capture C:\temp\capture-projects     # rebuild layer 2
python kb_index.py report                                       # overview and sample queries
python kb_index.py export <session-id-prefix> --out session.md  # one session as readable Markdown
python kb_index.py query "select * from v_timeline where session_id like 'a15b%'"
```

Defaults: transcripts from `~/.claude-capture/outbox`, database at
`~/.claude-capture/capture-index.db`. `--capture` takes a capture project or a folder of them,
and can be repeated (a local repository with `refs/claude-capture` works too).

Layer 2 is rebuilt on every `build`; layer 3 is never dropped and links to layer 2 by natural
keys (session ID, message UUID, tool-use ID, snapshot commit), so links survive rebuilds.
Views: `v_session_overview`, `v_timeline`, `v_file_history` (each change with the prompt that
preceded it). Snapshots taken at session start are marked as baselines: they record uncommitted
work that existed before the session, and are not counted as changes made in the session.

`test_index.sh` runs the end-to-end check (real snapshots from `capture.sh` plus transcripts in
Claude Code's format).

## Windows (verified setup)

`install.sh` is bash-only and does not run from PowerShell. Set up by hand:

1. Prerequisites (each must print a version): `claude --version`, `git --version`,
   `& "C:\Program Files\Git\bin\bash.exe" --version`, `python --version`.
   Use a current Claude Code (`claude update`); 2.0.76 failed to sign in (OAuth 404).
2. Copy the script:
   `New-Item -ItemType Directory -Force "$HOME\.claude-capture\bin"; Copy-Item .\capture.sh "$HOME\.claude-capture\bin\capture.sh"`
3. Open `notepad "$HOME\.claude\settings.json"` and paste `settings.windows.example.json`,
   replacing `YOUR-NAME` (3 places) and `CAPTURE_REMOTE_BASE`. Save with Notepad, not PowerShell
   (PowerShell 5 can add a byte-order mark that stops Claude Code reading the file).
4. Validate: `Get-Content "$HOME\.claude\settings.json" -Raw | ConvertFrom-Json | Out-Null; "valid"`
5. Watch the log: `Get-Content "$HOME\.claude-capture\capture.log" -Wait -Tail 50`
6. Start `claude` in a Git repository; `/hooks` should list 3 hooks; the log shows `[session-start]`.

Why the hook command calls Git Bash by full path: plain `bash` resolves to
`C:\Windows\System32\bash.exe` (Windows Subsystem for Linux), which cannot see `C:/...` paths
("No such file or directory"). The shell-form command (no `args` field) works on old and new
Claude Code versions; the `args` form is only understood by recent versions.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CAPTURE_REMOTE` | unset | Capture project URL for this repository |
| `CAPTURE_REMOTE_BASE` | unset | Capture group URL; project derived as `<base>/<repo>-capture.git` |
| `CAPTURE_HOME` | `~/.claude-capture` | State, log, pending queue and outbox |
| `CAPTURE_OUTBOX` | `$CAPTURE_HOME/outbox` | Where transcripts are copied (stands in for the ingestion endpoint) |
| `CAPTURE_SYNC` | unset | `1` pushes synchronously (used by the test) |

## Deliberate POC limits

- **Transcripts go to a local outbox**, not an ingestion service. Replacing the copy with an
  authenticated upload is the next step.
- **Secret detection is basic** without gitleaks: a handful of patterns (cloud keys, private
  keys, tokens, `password=` style assignments). Install gitleaks for real use. A secret found in
  unpushed commits keeps that snapshot local and logs it, rather than rewriting history.
- **One repository per session**: the working directory's repository. Directories added with
  `/add-dir` are not snapshotted yet.
- **Branch backup keeps the latest tip** of each branch (force-updated), not every version.
- **Snapshots are per response**, not per file edit. Adding a `PostToolUse` hook on `Edit|Write`
  would give per-edit snapshots at the cost of many more refs.
- **Sub-agent transcripts**: the script copies a folder named after the session next to the main
  transcript if one exists. Claude Code's actual layout needs confirming in a real session.
- **Hidden refs accumulate in the developer's local repository** under `refs/claude-capture`.
  A pruning step is not included.
- **macOS and Linux** (and Git Bash on Windows, untested). Deployment through managed settings
  with `allowManagedHooksOnly` is not part of the POC.
- **Developer notice**: capture includes everything in the working tree that is not ignored.
  Run this only on repositories and with developers covered by the employee notice.
~~~~

### A.9 `CLAUDE.md`

~~~~markdown
# claude-capture POC: context for Claude Code

Full background, decisions and open items are in HANDOVER-claude-interaction-capture.md
(in the Claude Project). This file is the short version Claude Code loads automatically.

## What this is
Local capture piece of a Xenovex client proposal ("Capturing Claude Interaction Context").
Goal: reusable answers for staff, and no loss of knowledge from Claude sessions, including
code state that lives only in local Git branches and uncommitted work.

## Agreed design (do not change without discussion)
- Claude Code hooks: SessionStart (session marker, baseline snapshot, branch backup, retry
  queue), Stop (after every Claude response: transcript copy + working-tree snapshot),
  SessionEnd (final transcript, snapshot, branch backup).
- The developer never commits. Snapshots: temporary index (GIT_INDEX_FILE), `git add -A`,
  `write-tree`, `commit-tree -p HEAD`, hidden ref `refs/claude-capture/sessions/<session_id>/<seq>`.
- First snapshot of a session (SessionStart) is a baseline: uncommitted work that existed
  before the session; not counted as changes made in the session.
- Branch backups: `refs/claude-capture/users/<email>/heads/*` (latest tip, force-updated).
- One capture project per source repository (`<repo>-capture`), derived from CAPTURE_REMOTE_BASE.
- Phase 1b (needs Claude Enterprise): Compliance API supplies chat/Cowork/M365/Chrome and an
  independent copy of Claude Code transcripts, matched via the session marker. Not in this repo.
- `kb_index.py` builds the structured index (layer 2) and empty knowledge tables (layer 3).

## Hard rules for capture.sh
- Always exit 0; never exit 2 (on Stop it would keep Claude from finishing).
- Never touch the developer's HEAD, branches, index or files.
- Respect .gitignore; scan changed files and unpushed commits for secrets; record exclusions.
- Keep hooks fast; queue failed pushes and retry at next session start.
- SessionEnd default budget is 1.5 s: keep explicit `timeout` in settings.
- Transcript file can lag: re-copied at next session start.
- Marker text (SessionStart additionalContext) must stay factual, not instruction-like.

## State
- test_poc.sh 27/27, test_index.sh 19/19 (simulated hook input).
- Verified live on Windows 11 with Claude Code (updated version): hooks via full Git Bash path,
  marker reaches Claude, per-response snapshots, push to a local stand-in capture project,
  `env` block in settings reaches hooks.

## Next steps
1. Confirm SessionEnd on Windows (Step 7); run kb_index.py on real transcripts and fix parser
   gaps (marker line type, sub-agent layout).
2. Test VS Code extension and Desktop app Code tab (local session).
3. Point CAPTURE_REMOTE_BASE at a real GitLab group; non-interactive credentials.
4. Replace outbox copy with authenticated upload to an ingestion endpoint.
5. gitleaks integration; multi-repo (/add-dir); optional per-edit snapshots; prune local refs;
   managed-settings packaging (allowManagedHooksOnly).
~~~~
