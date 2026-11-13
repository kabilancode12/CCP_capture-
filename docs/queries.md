# The six saved questions

The audit trail has to answer six questions. Each is a read-only **view** in PostgreSQL (migration 011), built
on the tables the [indexer](indexer.md) fills. A view needs no upkeep: it always shows the current index. Run
them in `psql` ([setup.md](setup.md)) or any SQL tool, and filter or sort them yourself.

Rewound work is **kept and flagged** (`is_abandoned`), because it really happened. Add `WHERE NOT is_abandoned`
to see only the conversation that continued. Times are `occurred_at` of the transcript entry (UTC).

| # | Question | View |
| --- | --- | --- |
| 1 | What did a session cost, and which models did it use? | `v_session_cost` |
| 2 | Every change to a file: who made it, and which prompt was behind it? | `v_file_history` |
| 3 | Which sub-agents ran, what were they told, what did they return? | `v_agent_run` |
| 4 | Which decisions did a person take (refusals, rewinds, plan approvals)? | `v_human_decision` |
| 5 | Where did a session compact? | `v_compaction` |
| 6 | Where did tests fail and then pass after edits? | `v_test_fix_cycle` |

## 1. Cost and models: `v_session_cost`

One row per session: `session_id`, `started_at`, `branch`, `status`, `total_cost_usd`,
`has_unknown_model_cost` (true means the total is partial), `models` (most expensive first), and the token
totals. Cost is read from the transcript's `cost-state`, never computed (so helper models are included).

```sql
SELECT session_id, started_at, total_cost_usd, models
  FROM v_session_cost ORDER BY total_cost_usd DESC NULLS LAST LIMIT 10;
```

## 2. File history: `v_file_history`

One row per AI edit: `path`, `session_id`, `occurred_at`, `tool`, `change_type`, `actor_class`, `user_modified`,
`lines_added`, `lines_removed`, `hunks` (the changed line ranges), `prompt` (the first 500 characters of the
developer's prompt) and the ids of the prompt, call and result events. `path` is relative to the session's
folder, so it is the same on every machine.

```sql
SELECT occurred_at, session_id, tool, lines_added, lines_removed, prompt
  FROM v_file_history
 WHERE path = 'src/app.py' AND NOT is_abandoned
 ORDER BY occurred_at;
```

Covers Edit and Write. Not covered: files changed by a shell command, and changes made by a person
([indexer.md](indexer.md#file-changes)). `actor_class` is `ai_edit` for every row today.

## 3. Sub-agents: `v_agent_run`

One row per sub-agent: `agent_type`, `description`, `spawn_depth`, `model`, `instructions` (what the parent told
it), `result` (its last reply), `started_at`, `ended_at`, `event_count`, and `parent_agent_id` (empty when the
main conversation launched it, otherwise the agent that did).

```sql
SELECT agent_type, instructions, result FROM v_agent_run WHERE session_id = '...' ORDER BY started_at;
```

## 4. Human decisions: `v_human_decision`

One row per decision: `session_id`, `decision_type`, `event_id`, `occurred_at`, `detail`, `tool`.

| `decision_type` | Meaning | `detail` |
| --- | --- | --- |
| `denial` | A tool call was refused (by a person, a permission rule, or the automatic mode) | `user-rejected`, `permission-rule`, `automode-blocked`, ... |
| `rewind` | The developer went back and typed a different prompt; this is the abandoned one | `abandoned prompt`. `occurred_at` is when the replacing prompt was typed |
| `plan_decision` | A plan was approved or rejected (`ExitPlanMode`) | `approved` or `rejected` |

**Unverified:** plan decisions have not appeared in any real transcript yet, so their shape is an assumption
(checked only on invented data). Verify them during the pilot.

```sql
SELECT decision_type, detail, tool, occurred_at FROM v_human_decision WHERE session_id = '...';
```

## 5. Compactions: `v_compaction`

One row per compaction: `occurred_at`, `trigger` (`manual` or `auto`), `pre_tokens`, `post_tokens`,
`prompts_before` (prompts the developer had typed by then, kept work only) and the `summary` text.

```sql
SELECT session_id, occurred_at, trigger, pre_tokens, post_tokens FROM v_compaction ORDER BY occurred_at;
```

## 6. Test-fix cycles: `v_test_fix_cycle`

For each **failed** test run, the next **passed** run of the same runner in the same session, and how many
(kept) file changes happened between them: `failed_command`, `failed_at`, `passed_at`, `edits_between`. A failure
that is never followed by a pass is not listed (look in `test_run` for those). The order is decided by time;
entries without a time are not matched. Outcomes come from exit codes, with the limits in
[indexer.md](indexer.md#test-runs).

```sql
SELECT session_id, framework, failed_at, passed_at, edits_between
  FROM v_test_fix_cycle ORDER BY failed_at DESC;
```

## How these are tested

[`tests/integration/test_saved_questions.py`](../tests/integration/test_saved_questions.py) indexes one invented
session with the real indexer (a fix, a failing then passing test run, a refused command, a compaction, a
rewind, a sub-agent, a cost entry) and checks each view against the answer that can be read off that story. It
also covers abandoned work, an unfinished fix, plan decisions and empty data.
