# Transcript facts

What Claude Code actually writes, found by running the [test-session script](test-session-script.md)
and inspecting the result with `ccp-verify`. Each row says how the answer changes the design.

- **Tested:** Claude Code **2.1.289**, Windows 11, terminal (PowerShell) and one headless run
  (`claude -p`). Hooks ran through Git Bash. One interactive session covered everything below.
- **Not tested:** Linux, macOS, VS Code, JetBrains, Desktop app, SSH, web. Cells say `not tested`
  where that applies; they are not guesses.
- All values were inspected as structure only (key names, types, lengths), never printed as content.

## Hooks

| # | Question | Observed (Windows) | Linux | Impact on design |
| --- | --- | --- | --- | --- |
| 1 | Which hooks fire, and in what order? | All ten probed names are accepted. Fired: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop, PreCompact, Notification, SessionEnd. Normal turn: UserPromptSubmit, (PreToolUse, PostToolUse)*, Stop | not tested | The planned five hooks all exist. Add PostToolUse only if per-tool capture is needed |
| 2 | Does SessionEnd fire? | Yes: headless run (`reason=other`) and interactive `/exit` (`reason=prompt_input_exit`). Window close and crash not tested | not tested | Still best-effort: upload on Stop and SubagentStop so nothing depends on SessionEnd |
| 3 | Is SessionStart re-run? | Yes: after `/compact` it fires again in the same session with `source=compact`. Also `startup` and `resume` | not tested | The hook must be idempotent: do not create a new capture id or baseline snapshot every time. `source` says why it ran |
| 4 | Is `Stop` always sent? | No: it did **not** fire after a turn ended by a permission denial. It also fires while an async sub-agent is still running | not tested | An interrupted turn skips Stop. Catch up on the next prompt, SubagentStop or SessionEnd; never assume Stop means all work is done |
| 5 | Do sub-agent hooks fire? | Yes. `SubagentStart` has `agent_type` and `agent_id`. `SubagentStop` has `agent_transcript_path`. PreToolUse/PostToolUse inside a sub-agent also carry `agent_type` and `agent_id` | not tested | Tool calls can be attributed to a sub-agent live. Upload the sub-agent file at SubagentStop |
| 6 | Is every SubagentStop a real sub-agent? | No: a `SubagentStop` with **empty `agent_type`** and no transcript file fires after most turns (an internal helper, probably the title job, using claude-haiku) | not tested | Treat a sub-agent as real only if `agent_type` is non-empty and the file exists |
| 7 | Do transcripts exist when the hook runs? | Not at SessionStart (`startup`) or at the first UserPromptSubmit. They exist from the first tool call, and already at `resume` | not tested | SessionStart cannot read the transcript. Copy at Stop and catch up later |
| 8 | What is in hook payloads? | Common: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `scratchpad_dir`. `UserPromptSubmit`: `prompt`, `prompt_id`, `permission_mode`. `Stop`: `last_assistant_message`, `effort`, `background_tasks`, `session_crons`, `stop_hook_active`. `Pre/PostToolUse`: `tool_name`, `tool_input`, `tool_use_id` (+ `tool_response`, `duration_ms` after). `PreCompact`: `trigger`, `custom_instructions`. `SessionStart`: `source`, `model`. `SessionEnd`: `reason` | not tested | Payloads contain content (prompts, replies, tool input): redact them if ever stored |
| 9 | Do permission decisions have a hook? | Not among the ten probed. `Notification` fired for a slow approval (6 s) but not for a quick denial; it also fires as an idle reminder after 60 s | not tested | Do not use Notification to detect prompts. Decide from the transcript (row 18). Check whether a dedicated permission hook exists |

## Transcript content

| # | Question | Observed (Windows) | Linux | Impact on design |
| --- | --- | --- | --- | --- |
| 10 | Are thinking blocks written? | A `thinking` block is written when the model thinks, but its **text is empty**: only the `signature` is present. No `redacted_thinking`. A trivial prompt produced none. None in the sub-agent | not tested | The `thinking` table holds "present + signature" only. No reasoning text can be stored. `cost-state` gives `thinkingTokens` per model |
| 11 | Do Edit results carry structured patch data? | Yes: `toolUseResult` has `structuredPatch` (`oldStart`, `oldLines`, `newStart`, `newLines`, `lines`), plus `oldString`, `newString`, `originalFile`, `userModified` | not tested | Per-edit line ranges are free: per-edit snapshots are not needed for AI edits. `originalFile` is the whole file: redact and mind the size |
| 12 | Do assistant entries carry model and usage? | Yes: `model` and `usage` (input, output and cache tokens) on every assistant entry | not tested | Feeds `model_usage` |
| 13 | Is cost recorded? | Yes: `cost-state` entries hold `totalCostUSD`, per-model `modelUsage` (tokens, `thinkingTokens`, `costUSD`), lines added/removed and durations. A second model (claude-haiku) appears for helper work | not tested | Cost is read, not computed: no price table needed |
| 14 | Are tool-result shapes uniform? | No: `toolUseResult` is an object for Glob, Read, Edit and a successful Bash, but a **plain string** for a failed Bash and for a denial | not tested | The indexer must accept both shapes |
| 15 | What entry types exist? | `user`, `assistant`, `system` (`stop_hook_summary`, `turn_duration`, `compact_boundary`), `attachment`, `mode`, `permission-mode`, `atis-latch`, `last-prompt`, `ai-title`, `cost-state`, `file-history-snapshot`, `file-history-delta`, `queue-operation`. Many keys per entry | not tested | The indexer must ignore unknown types safely. New types will appear |
| 16 | How big are transcripts? | 565 KB for about 10 minutes and 31 user entries; **83 % of the bytes are `attachment` entries** (skill, tool and agent listings, a 147 KB `prompt_snapshot`). A resume adds a further ~200 KB of attachments | not tested | Storage and upload volume are dominated by repeated attachments. Consider de-duplicating attachments by hash. They may contain CLAUDE.md or memory text: redact |

## Sessions, tree and sub-agents

| # | Question | Observed (Windows) | Linux | Impact on design |
| --- | --- | --- | --- | --- |
| 17 | How are sub-agents stored and linked? | Separate files: `<session-id>/subagents/agent-<agent-id>.jsonl` and `.meta.json` (`agentType`, `description`, `toolUseId`, `spawnDepth`, ...). Entries have `isSidechain: true`, the parent `sessionId` and an `agentId`. The parent's tool is named **`Agent`** (input `description`, `prompt`, `subagent_type`); its result holds `agentId` and `outputFile`; it ran asynchronously | not tested | `meta.toolUseId` links the sub-agent to the parent call. Copy the whole session folder. Look for the `Agent` tool name |
| 18 | How is a permission decision recorded? | **Approval:** no entry of its own: PreToolUse then PostToolUse. **Denial:** PreToolUse, **no PostToolUse**, a tool-result entry with `is_error: true`, a `toolDenialKind` key and a string result. `permission-mode` entries record the mode (`auto`, `default`) | not tested | Decisions derive from the transcript: denied = `toolDenialKind`; approved = executed in a non-auto mode. In `auto` mode no prompt appears at all |
| 19 | How does a rewind appear? | The old prompt and everything after it stay in the file. The new prompt is a **second user-prompt child of the same parent entry**. No hook fires. `file-history-snapshot` / `file-history-delta` entries hold checkpoint backups; `last-prompt` entries have a `leafUuid` (probably the active tip). Tool calls also give a parent two children, so only user prompts count | not tested | Rewind = an entry followed by two or more user prompts. Mark the older branch as abandoned. Check `leafUuid` for the active tip `[verify]`. Confirm which restore option the rewind used |
| 20 | How is compaction recorded? | Same file and same session id. `PreCompact` (`trigger=manual`), then a `system` entry `compact_boundary` with `compactMetadata` (`trigger`, `preTokens`, `postTokens`, dropped tokens, `preservedMessages`) and a user entry with `isCompactSummary`. The boundary has `parentUuid: null` plus `logicalParentUuid`. Earlier messages stay in the file | not tested | Raw capture stays complete. The tree breaks at the boundary: follow `logicalParentUuid` |
| 21 | Does a resume keep the session id? | Yes: same id, same transcript file appended, `SessionStart source=resume` | not tested | No new session for a resume: `session_link` is only needed for other relations (for example after `clear`) `[verify]` |

## Identity and environment

| # | Question | Observed (Windows) | Linux | Impact on design |
| --- | --- | --- | --- | --- |
| 22 | Where are account, organisation and auth type stored? | `~/.claude.json` key `oauthAccount`: `emailAddress`, `displayName`, `fullName`, `organizationName`, `organizationUuid`, `organizationType`, `organizationRole`, `billingType`, `seatTier`, `accountUuid`. Also in the transcript: `session_context.userEmail`, `credential_org.organizationUuid`, `environment` (platform, shell, OS version). `ANTHROPIC_API_KEY` is the API-key marker (presence only) | not tested | `session_env` can be filled automatically, with no developer registration. This is personal data: restrict access |
| 23 | How are surface and run mode told apart? | `CLAUDE_CODE_ENTRYPOINT` is set in hook and transcript data: `cli` in a PowerShell terminal, `claude-vscode` in the VS Code extension. `TERM_PROGRAM` is not set by the extension. It is set **only in processes Claude Code starts**: running `ccp-verify env` by hand in plain PowerShell shows it as not set | not tested | Detect the surface inside the SessionStart hook, never from a manual run. Other values (JetBrains, Desktop, web, SDK) not yet seen |
| 24 | Can the shell be identified? | Hooks run in Git Bash (`MSYSTEM` set), even when the user works in PowerShell. In a manual PowerShell run, cmd and PowerShell cannot be told apart from environment variables. The transcript's `environment` entry states Claude's tool shell as a sentence (`PowerShell (primary); Bash tool also available ...`), plus `platform` and `osVersion` | not tested | Read the shell from the transcript's `environment` entry (parse loosely: it is prose); record the hook's own shell separately |
| 25 | Do hooks run on every surface? | Windows terminal (interactive) and headless: yes. VS Code extension, JetBrains, Desktop, SSH, web: not tested | not tested | Test each surface with the probe before relying on it |
| 26 | Do git notes survive our GitLab push and merge flow? | not tested (needs the GitLab server) | not tested | Decides how reliable notes-based commit linking is |

## Open follow-ups

- Which restore option was used in the rewind (conversation only, or code and conversation)?
- Is there a dedicated permission-request hook? (Probe it by adding its name to `PROBE_EVENTS`.)
- Repeat the session on Linux and in the VS Code extension.
