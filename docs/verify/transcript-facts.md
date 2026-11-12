# Transcript facts

Fill this table from the output of `ccp-verify` after running the
[test-session script](test-session-script.md). One row per `[verify]` item. The last column is
what makes the facts useful: say how the answer changes the design.

Leave a cell as `?` until it has been observed. Do not guess.

| # | Question | Observed (Windows) | Observed (Linux) | Claude Code version | Impact on design |
| --- | --- | --- | --- | --- | --- |
| 1 | Are thinking blocks written? With text, or signature only? Any `redacted_thinking`? | ? | ? | ? | Decides what the `thinking` table can hold |
| 2 | Do Edit/Write results carry structured patch data (`toolUseResult`)? | ? | ? | ? | If yes, per-edit line ranges are free; per-edit snapshots are unnecessary |
| 3 | Do assistant messages include `model` and token `usage`? | ? | ? | ? | Feeds `model_usage` and cost |
| 4 | Sub-agents: inline (`isSidechain`) or separate files? How are they linked to the parent Task call? | ? | ? | ? | Decides how `agent_run` is captured |
| 5 | Do SubagentStart / SubagentStop hooks fire, with an agent id and transcript path? | ? | ? | ? | Decides whether sub-agent transcripts are copied promptly |
| 6 | How does a rewind appear (branching `parentUuid`, checkpoint entries)? | ? | ? | ? | Needed to mark abandoned branches |
| 7 | Does a resume keep the session id or create a new one? | ? | ? | ? | Decides how `session_link` is built |
| 8 | How is compaction recorded (entry type, trigger manual/auto)? | ? | ? | ? | Feeds the `compaction` table |
| 9 | Does SessionEnd fire reliably (especially on Windows)? | Partly: fired once in headless `claude -p` (Git Bash hook, `reason=other`). Interactive `/exit`, window close and crash not yet tested | ? | 2.1.289 | Design already treats it as best-effort; confirms how much data is at risk |
| 10 | Where are account name, email, organisation and auth type stored locally? Same on every surface? | ? | ? | ? | Feeds `session_env`; may need one-time developer registration |
| 11 | Can run mode, surface, shell and access path be told apart? | ? | ? | ? | Feeds `session_env`; see the environment matrix |
| 12 | Do hooks run in every surface, including headless and web? | Headless `claude -p` on Windows: SessionStart, UserPromptSubmit, Stop and SessionEnd fired. Other surfaces not yet tested | ? | 2.1.289 | Decides where capture works without extra integration |
| 14 | Which keys do hook payloads carry? (found by the hook probe) | Common: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `scratchpad_dir`. `UserPromptSubmit` adds `prompt`, `prompt_id`, `permission_mode`. `Stop` adds `last_assistant_message`, `effort`, `background_tasks`, `session_crons`, `stop_hook_active`. `SessionEnd` adds `reason`. `SessionStart` adds `source` | ? | 2.1.289 | Payloads contain content (prompt, last reply): redact hook payloads too if they are ever stored. `last_assistant_message` is a cheap per-turn capture even if the transcript lags |
| 15 | Are all ten probe event names accepted in settings (incl. SubagentStart, Notification)? | Yes: no settings error. Not all fired in the headless test (no tools, sub-agents or compaction) | ? | 2.1.289 | Names are valid; whether SubagentStart/Stop fire is tested in the full session |
| 13 | Do git notes survive our GitLab push and merge flow? | ? | ? | ? | Decides how reliable notes-based commit linking is |
