# Claude Interaction Capture: Next Steps and Capability Gaps

**Owner:** Thiagarajan Ramani · **Status date:** 11 November 2026
**Classification:** Xenovex internal and confidential. Project team only.
Read together with `HANDOVER-claude-interaction-capture.md` (design, decisions, POC state).

This file has three parts:
- **Part A:** tasks carried over from the handover (section 11).
- **Part B:** an assessment of seven capture questions raised on 11 Nov, with the work each one needs.
- **Part C:** one consolidated, ordered backlog.

Items marked **[verify]** depend on Claude Code behaviour that has to be confirmed on real
transcripts before we build on it.

---

## Part A: Carried over from the handover

### POC
- [ ] A1. Confirm SessionEnd on Windows (Step 7: `/exit`, then check the `[session-end]` log lines, the final snapshot and the push).
- [ ] A2. Run `kb_index.py` on real transcripts; fix parser gaps (marker line type, sub-agent layout).
- [ ] A3. Test the VS Code extension and the Desktop app Code tab (local session); fill in the coverage table (handover §10).
- [ ] A4. Real GitLab: create the group and a `<repo>-capture` project; non-interactive credentials; server-side protection hook; automatic creation of capture projects.
- [ ] A5. Test background pushing with `CAPTURE_SYNC` off.
- [ ] A6. Replace the outbox with an authenticated, incremental upload to an ingestion endpoint.
- [ ] A7. gitleaks; multi-repo (`/add-dir`); prune local refs; package as managed settings with `allowManagedHooksOnly`.

### Proposal and client
- [ ] A8. Fix the five doc gaps (handover §5.1); export V1.1 and send it with the replies to the review comments.
- [ ] A9. Questions for Anthropic: knowledge-management use of the Compliance API; how sub-agent work is recorded; whether thinking content is included (new, see B3).
- [ ] A10. Client decisions and open pre-requisites (handover §11).

---

## Part B: Capability assessment

Summary:

| # | Question | Today | Main gap |
| --- | --- | --- | --- |
| 1 | Which lines changed in a file | **Mostly yes** | The index stores only line counts, not which lines; several edits in one response are merged into one snapshot |
| 2 | Why a file or line changed (annotate) | **Partly** | Snapshots aren't chained, so blame doesn't work inside a session; developer commits have no link back to sessions |
| 3 | Brainstorming before the answer | **Partly** | Visible reasoning is captured; the indexer drops thinking blocks |
| 4 | Chain of thought | **Partly, and only partly possible** | Only what Claude Code writes to the transcript (summarised or redacted thinking) can ever be captured |
| 5 | Test runs and regenerated code | **Mostly yes, in the transcript** | Not classified or linked; edits between snapshots aren't in Git |
| 6 | How we arrived at a piece of code | **Raw material yes, view no** | Needs a provenance chain from line to prompt, with the reasoning, inputs and tests behind it |
| 7 | Agents, sub-agents, persona, memory | **Partly** | Sub-agent transcript layout, worktree isolation, agent definitions and memory files aren't handled |

General principle: `capture.sh` copies the whole raw transcript, so anything Claude Code
writes there is already in layer 1. Most gaps are in **what we snapshot** (Git granularity
and history shape) and **what we index** (`kb_index.py`), not in raw capture. Raw data
captured now can be re-indexed later, because layer 2 is rebuildable.

### B1. Given a file, which lines were changed?

**Captured today:** each snapshot is a full tree, so `git diff <start commit> <snapshot>` or
`git diff <snapshot n-1> <snapshot n>` gives exact hunks and line numbers on demand.
`kb_index.py` records added/modified/deleted and +/- line counts (`file_change`). Each Edit
tool call in the transcript holds the exact old and new text.

**Gaps:**
- The index has no line ranges or hunks, so "which lines" needs a Git call each time.
- Snapshots are taken once per response. Five edits to one file in one response appear as one change in Git; only the transcript shows the individual edits.
- A snapshot includes the developer's own edits made between responses, mixed with Claude's.

**Tasks:**
- [ ] B1.1 Add a `hunk` table (file, old/new start line, old/new line count, snapshot), filled from `git diff -U0`.
- [ ] B1.2 Optional per-edit snapshots: a PostToolUse hook on `Edit|Write|MultiEdit|NotebookEdit`. Measure the cost (refs, push volume, latency) on a real session first.
- [ ] B1.3 Separate Claude's changes from the developer's: compare each snapshot diff with the Edit/Write tool calls made in that response, and flag hunks that no tool call explains as "developer edit".
- [ ] B1.4 [verify] Check whether transcript lines carry a structured result for edits (Claude Code appears to write a `toolUseResult` with patch detail). If so, index it; it gives per-edit line ranges without extra snapshots.

### B2. Why was a file or line changed? (Git annotate / blame)

**Captured today:** `v_file_history` links each file change to the prompt that came before
it (file level, by timestamp).

**Gaps:**
- **Snapshots don't form a chain.** `capture.sh` creates every snapshot with the developer's HEAD as its parent (`commit-tree -p HEAD`). So snapshots in a session are siblings, and `git blame` on the last snapshot credits every line changed in the session to that last snapshot. Blame needs each snapshot to build on the previous one.
- **Committed code has no link back to a session.** In the main repository, blame shows the developer's commit, with no link to the Claude session that produced the code.
- The snapshot commit message records session and sequence but not the prompt (deliberately: it's metadata only).

**Tasks:**
- [ ] B2.1 **(Design change: discuss before implementing.)** Chain the snapshots: parent = the session's previous snapshot. If the developer has committed since then, add HEAD as a second parent. The baseline keeps HEAD as its only parent. Then `git blame <final snapshot> -- file` attributes each line to the response that produced it. Tests: update `test_poc.sh` (step 5 currently expects parent = the unpushed commit) and the diff base in `kb_index.py`.
- [ ] B2.2 Line-to-reason lookup: blame → snapshot commit → session + sequence number → prompt for that response, Claude's explanatory text and the tool calls (from the index). Expose it as `kb_index.py why <file> <line>`.
- [ ] B2.3 Link developer commits to sessions **without changing them**: match file contents (blob IDs) in the developer's commits and merge requests against blob IDs in snapshots. Identical content means the code came from that snapshot. This keeps the "nothing the developer owns is modified" principle.
- [ ] B2.4 Alternative to B2.3, for discussion: a `prepare-commit-msg` hook that adds a `Claude-Session:` trailer. It's simpler, but it modifies developer commits, so it needs client agreement.
- [ ] B2.5 Merge-request view: for a merge request, list the sessions whose snapshots contributed to it (uses B2.3).

### B3. Brainstorming and working-out before the answer

**Captured today:** the raw transcript holds every assistant message, including the
working-out Claude writes between tool calls ("I'll check X first…"), plan-mode plans (as the
plan tool's input), and to-do lists (TodoWrite tool calls). `kb_index.py` indexes the text
blocks and tool calls.

**Gaps:**
- `kb_index.py`'s `content_text()` keeps only `text` and `tool_result` blocks. **Thinking blocks are dropped from the index**, though they remain in the raw transcript if Claude Code wrote them.
- Plans and to-do lists are indexed as generic tool calls, not as first-class "plan" records.

**Tasks:**
- [ ] B3.1 [verify] On real transcripts, with extended thinking on and off: check whether `thinking` blocks are written, whether they contain text or only a signature, and whether `redacted_thinking` blocks appear.
- [ ] B3.2 Index thinking as its own message kind (`thinking`) with a `redacted` flag; keep it out of prompt and response counts.
- [ ] B3.3 Index plans (plan-mode output) and to-do lists as `plan` records linked to the response that produced them. These are often the clearest "approach" record in a session.
- [ ] B3.4 Curation rule: thinking may be used to help **distil** a knowledge entry, but is never published as the rationale. See B4.

### B4. Chain of thought

**Position to agree internally and with the client:**
- We can capture only what Claude Code writes to the transcript (and, in Phase 1b, what the Compliance API returns). Nothing else of the model's internal reasoning is available to any client tool.
- For current models, the thinking returned is generally a **summary** of the model's reasoning, not the full internal process, and parts may be **redacted** (encrypted). Whether thinking text is written to the transcript depends on the Claude Code version and settings ([verify], B3.1).
- Even when present, thinking is not a guaranteed faithful account of why the model acted. It's useful context for reviewers, not evidence. Evidence is what happened: tests passing, code surviving into a merge, user confirmation (handover §3.7).
- Governance: thinking can contain sensitive content (credentials seen in files, personal data). It needs the same redaction as the transcript, and possibly tighter access.

**Tasks:**
- [ ] B4.1 Covered by B3.1 and B3.2 (verify and index).
- [ ] B4.2 Add a question for Anthropic (A9): is thinking content included in Compliance API session records, and in what form?
- [ ] B4.3 Proposal doc: add a short paragraph on what "reasoning capture" means and its limits. Add it as an Assumption / Open Point rather than a promise.

### B5. Test runs and regenerated code

**Captured today:**
- Every Bash or PowerShell tool call (test commands included) with its command, output (`result_text`) and error flag.
- Every Edit/Write, including rewrites of the same code.
- Snapshots of the end state of each response.

**Gaps:**
- Test runs aren't identified or parsed (pass/fail counts, failing test names).
- Code regenerated several times within one response appears in Git only as the final version; the intermediate versions are only in the transcript (B1.2 would close this).
- The indexer truncates tool input and results to 20,000 characters; long test logs are cut.
- **Rewind/checkpoints:** if the developer rewinds (Claude Code checkpoints) or edits an earlier prompt, the transcript branches (`parentUuid` tree). The indexer treats messages as one linear sequence, so abandoned branches look like part of the main path. [verify] whether checkpoint entries appear in the transcript (for example as file-history snapshot lines).

**Tasks:**
- [ ] B5.1 Classify test commands (pytest, npm/yarn/pnpm test, mvn/gradle test, go test, dotnet test, jest, etc.) into a `test_run` table: command, pass/fail/error counts, failing tests, duration, and the snapshot it ran against.
- [ ] B5.2 "Fix loop" detection: test fails → edit → test passes, linked to the edits in between. This is strong evidence for the knowledge base (handover §3.7).
- [ ] B5.3 Keep full tool results in layer 1 (already there); raise or remove the truncation limit in layer 2, or store large results as a reference to the raw line.
- [ ] B5.4 Rebuild the conversation tree from `uuid`/`parentUuid`, and mark abandoned branches and rewinds so curation ignores them.

### B6. How did we arrive at a piece of code? What contributed to the change?

The answer is a provenance chain. Most of the raw material is already captured; we need to
assemble it:

line/hunk → snapshot (B2.1 blame) → session and response → **prompt** → Claude's visible
reasoning, plan, to-do list and thinking (B3) → **inputs consulted**: files read (Read, Grep, Glob
calls), web pages (WebFetch, WebSearch), MCP tool results, CLAUDE.md/memory in effect,
sub-agent results (B7) → **attempts**: edits, test runs and failures (B5) → final snapshot →
developer commit and merge request (B2.3) → knowledge entry (layer 3).

**Tasks:**
- [ ] B6.1 Index "inputs consulted": an `input_source` table (type file/web/MCP/sub-agent/memory, identifier, tool-use ID), extracted from tool calls.
- [ ] B6.2 Record the CLAUDE.md and memory files in effect at session start (hash and content reference). They shape the result but aren't in the conversation as the user's words. Repository-level files are in the snapshot; user-level files (`~/.claude/CLAUDE.md`) need copying at SessionStart. Apply secret scanning.
- [ ] B6.3 `kb_index.py provenance <file> <line>`: prints the chain above as readable Markdown. Depends on B1.1, B2.1/B2.2, B3.2, B5.1 and B6.1.
- [ ] B6.4 Use the provenance output as the input to the Phase 2 "distil" step, so knowledge entries cite their evidence.

### B7. Agents and sub-agents: code, thoughts, persona, memory

**What happens today:**
- **Code:** sub-agents usually work in the same working tree as the parent, so their file changes show up in the parent session's snapshots, credited to the parent session. With **worktree isolation**, a sub-agent edits a separate Git worktree. `capture.sh` snapshots only the session's working directory, so that work is **missed** until it's merged back.
- **Conversation:** older Claude Code versions write sub-agent messages inline with `isSidechain: true` (indexed and flagged). Newer versions appear to write each sub-agent's transcript to a separate file under a folder named after the session; `capture.sh` copies that folder if it exists, but `kb_index.py` doesn't parse it yet. [verify]
- **Thoughts:** a sub-agent's thinking is in its own transcript, under the same rules and limits as B3/B4.
- **Persona:** a sub-agent's persona is its definition file: a Markdown file with a system prompt, allowed tools and model, under `.claude/agents/` (repository) or `~/.claude/agents/` (user). Repository-level definitions are in the snapshot. User-level ones and plugin-supplied ones are not captured.
- **Memory:**
  - Claude Code memory is file-based (CLAUDE.md at project, user and local level; `CLAUDE.local.md` is often git-ignored and therefore excluded).
  - Sub-agents have no persistent memory by default; recent versions may add optional per-agent memory folders. [verify]
  - Agents built by the client with the **Claude Agent SDK** don't use the developer's settings hooks unless the application registers hooks itself. They need their own capture integration.
- **Parallel agents** (several Claude Code instances, agent teams) each have their own session ID and are captured as separate sessions, as agreed in handover §3.5.

**Tasks:**
- [ ] B7.1 [verify] In a real session with the Task/Agent tool, record the exact sub-agent transcript layout and whether `SubagentStart`/`SubagentStop` hooks fire with an agent ID and agent transcript path.
- [ ] B7.2 Register a `SubagentStop` hook to copy the sub-agent transcript promptly (`capture.sh subagent` mode) rather than waiting for the parent's Stop.
- [ ] B7.3 Index sub-agents: an `agent_run` table (agent ID, type/name, parent session, parent tool-use ID that launched it, prompt given, result returned, model), with its messages, tool calls and thinking linked to it.
- [ ] B7.4 Capture the persona: at session start and on SubagentStop, store the content hash and a copy of the agent definition that was used (repository, user or plugin), with secret scanning.
- [ ] B7.5 Capture memory: copy the CLAUDE.md files in effect and any agent memory folders at session start and end, versioned by content hash (shared with B6.2).
- [ ] B7.6 Worktree isolation: detect Git worktrees created during the session (`git worktree list`) and snapshot them under the same session, with a per-worktree ref namespace.
- [ ] B7.7 Agent SDK applications: write a short integration note (register equivalent hooks in code; same capture format) so client-built agents feed the same pipeline.
- [ ] B7.8 Proposal doc: extend "What counts as a session" with sub-agents, worktrees, personas and memory.

---

## Part C: Consolidated backlog (suggested order)

**Step 1: verify on real sessions (no code changes)**
1. A1 SessionEnd on Windows.
2. B3.1 thinking blocks; B1.4 `toolUseResult`; B5.4 rewind entries; B7.1 sub-agent layout and hooks. One scripted test session covering extended thinking, a sub-agent, a test run that fails then passes, and a rewind.
3. A2 run `kb_index.py` on those transcripts.

**Step 2: indexer improvements (layer 2 only; no change on developer machines)**
4. B3.2 / B3.3 thinking and plan records.
5. B1.1 hunks; B5.1 / B5.2 test runs and fix loops; B5.3 truncation; B5.4 conversation tree.
6. B6.1 inputs consulted; B7.3 sub-agent runs.

**Step 3: capture changes (`capture.sh`; agree first because they change the design)**
7. B2.1 chained snapshots (+ test updates).
8. B7.2 SubagentStop hook; B7.4 / B7.5 / B6.2 persona and memory capture; B7.6 worktrees.
9. B1.2 per-edit snapshots (after measuring cost); B1.3 Claude-versus-developer attribution.

**Step 4: provenance and linkage**
10. B2.2 `why`; B2.3 blob matching to developer commits; B2.5 merge-request view.
11. B6.3 `provenance`; B6.4 feed into curation.

**Step 5: platform and proposal**
12. A3–A7 (surfaces, GitLab, ingestion, packaging).
13. A8 doc V1.1 including B4.3 and B7.8; A9 questions to Anthropic (including B4.2); A10 client decisions.
