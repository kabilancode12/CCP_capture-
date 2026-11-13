"""The dashboard, as data: cards (SQL questions) and where they sit.

Every query reads one of the dashboard views of migration 013 (``d_session``, ``d_model_cost``,
``d_edit``, ``d_test_run``, ``d_fix_loop``) and nothing else. They hold counts, costs and short
categorical text; never prompts, replies or file contents. Changing a card here and running
``ccp-dashboards setup`` again updates it in Metabase.

The dashboard is a grid 24 columns wide; ``row`` and ``col`` start at 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COLLECTION_NAME = "CCP"
DASHBOARD_NAME = "Claude Code usage"
DASHBOARD_DESCRIPTION = (
    "Sessions, cost, models, edits and test fix loops, from the captured Claude Code sessions."
)


@dataclass(frozen=True)
class Card:
    """One question and how it is shown."""

    name: str
    sql: str
    display: str  # scalar | bar | line | pie | table | row | area
    row: int
    col: int
    width: int
    height: int
    description: str = ""
    settings: dict[str, Any] = field(default_factory=dict)


CARDS: tuple[Card, ...] = (
    # ------------------------------------------------------------------ headline numbers
    Card(
        "Sessions",
        "SELECT count(*) AS sessions FROM d_session",
        "scalar",
        0,
        0,
        6,
        3,
        "Sessions captured so far.",
    ),
    Card(
        "Total cost (USD)",
        "SELECT round(COALESCE(sum(total_cost_usd), 0), 2) AS cost_usd FROM d_session",
        "scalar",
        0,
        6,
        6,
        3,
        "Cost as recorded by Claude Code, helper models included.",
    ),
    Card(
        "Lines added by AI edits",
        "SELECT COALESCE(sum(lines_added), 0) AS lines_added FROM d_edit WHERE NOT is_abandoned",
        "scalar",
        0,
        12,
        6,
        3,
        "Lines added by Edit and Write, leaving out rewound work.",
    ),
    Card(
        "Test pass rate",
        "SELECT round(100.0 * count(*) FILTER (WHERE passed) / NULLIF(count(*) FILTER (WHERE passed IS NOT NULL), 0), 1) AS pass_percent FROM d_test_run",
        "scalar",
        0,
        18,
        6,
        3,
        "Share of test runs with a known outcome that passed.",
    ),
    # -------------------------------------------------------------------- usage over time
    Card(
        "Sessions per day",
        "SELECT day, count(*) AS sessions FROM d_session WHERE day IS NOT NULL GROUP BY day ORDER BY day",
        "bar",
        3,
        0,
        12,
        6,
    ),
    Card(
        "Cost per day (USD)",
        "SELECT day, round(sum(total_cost_usd), 2) AS cost_usd FROM d_session WHERE day IS NOT NULL GROUP BY day ORDER BY day",
        "line",
        3,
        12,
        12,
        6,
    ),
    Card(
        "Cost by model (USD)",
        "SELECT model, round(sum(cost_usd), 2) AS cost_usd FROM d_model_cost GROUP BY model ORDER BY cost_usd DESC NULLS LAST",
        "pie",
        9,
        0,
        8,
        6,
        "Includes the small helper model that writes session titles.",
    ),
    Card(
        "Sessions by surface",
        "SELECT surface, count(*) AS sessions FROM d_session GROUP BY surface ORDER BY sessions DESC",
        "pie",
        9,
        8,
        8,
        6,
        "Terminal, VS Code and so on, from the environment record.",
    ),
    Card(
        "Claude Code versions",
        "SELECT COALESCE(claude_code_version, 'unknown') AS version, count(*) AS sessions FROM d_session GROUP BY 1 ORDER BY sessions DESC",
        "bar",
        9,
        16,
        8,
        6,
    ),
    # ------------------------------------------------------------------------ the work done
    Card(
        "Lines changed per day, by actor",
        "SELECT day, actor_class, sum(lines_added) AS lines_added FROM d_edit WHERE NOT is_abandoned AND day IS NOT NULL GROUP BY day, actor_class ORDER BY day",
        "bar",
        15,
        0,
        12,
        6,
        "Today only AI edits are recorded (actor class ai_edit); shell-command and human changes follow.",
    ),
    Card(
        "Edits by tool",
        "SELECT COALESCE(tool, 'unknown') AS tool, count(*) AS edits FROM d_edit WHERE NOT is_abandoned GROUP BY 1 ORDER BY edits DESC",
        "bar",
        15,
        12,
        6,
        6,
    ),
    Card(
        "Edits changed by the developer",
        "SELECT CASE WHEN user_modified THEN 'changed by the developer' ELSE 'accepted as proposed' END AS edit, count(*) AS edits FROM d_edit WHERE NOT is_abandoned AND user_modified IS NOT NULL GROUP BY 1",
        "pie",
        15,
        18,
        6,
        6,
        "How often the developer altered the AI's proposal before accepting it.",
    ),
    # --------------------------------------------------------------- fix loops and decisions
    Card(
        "Test runs per day",
        "SELECT day, CASE WHEN passed THEN 'passed' WHEN passed = FALSE THEN 'failed' ELSE 'unknown' END AS outcome, count(*) AS runs FROM d_test_run WHERE day IS NOT NULL GROUP BY day, outcome ORDER BY day",
        "bar",
        21,
        0,
        12,
        6,
    ),
    Card(
        "Fix loops by test runner",
        "SELECT framework, count(*) AS loops, round(avg(edits_between), 1) AS avg_edits, round(avg(minutes_to_fix), 1) AS avg_minutes FROM d_fix_loop GROUP BY framework ORDER BY loops DESC",
        "table",
        21,
        12,
        12,
        6,
        "A failed test run followed by a pass: how many edits and minutes it took.",
    ),
    Card(
        "Human interventions per day",
        "SELECT day, sum(rewinds) AS rewinds, sum(denials) AS refused_tool_calls, sum(compactions) AS compactions FROM d_session WHERE day IS NOT NULL GROUP BY day ORDER BY day",
        "bar",
        27,
        0,
        12,
        6,
        "Rewinds, refused tool calls and compactions.",
    ),
    Card(
        "Most expensive sessions",
        "SELECT left(session_id, 8) AS session, day, surface, round(total_cost_usd, 2) AS cost_usd, prompts, file_changes, test_failures, commits FROM d_session WHERE total_cost_usd IS NOT NULL ORDER BY total_cost_usd DESC LIMIT 15",
        "table",
        27,
        12,
        12,
        6,
    ),
)
