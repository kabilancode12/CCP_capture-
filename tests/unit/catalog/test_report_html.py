"""The designed web page: thinking, options and decisions per conversation; safe and self-contained."""

from ccp.catalog.loader import ChatData
from ccp.catalog.report_html import (
    THEMES,
    assign_decisions,
    build_turns,
    markdown,
    render_report_html,
)

from .test_catalog_report import KEY, T0, _data, _event


def _turn_data() -> ChatData:
    data = _data()
    ask = {
        "questions": [
            {
                "question": "Which parser?",
                "header": "Parser",
                "multiSelect": False,
                "options": [
                    {"label": "Simple", "description": "small and fast"},
                    {"label": "Streaming", "description": "handles big files"},
                ],
            }
        ]
    }
    plan = {"plan": "# Plan\n\n1. add the parser\n2. test it"}
    asked = [{"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": ask}]
    answered = [{"type": "tool_result", "tool_use_id": "q1", "content": "Answer: Streaming"}]
    planned = [{"type": "tool_use", "id": "p1", "name": "ExitPlanMode", "input": plan}]
    approved = [{"type": "tool_result", "tool_use_id": "p1", "content": "approved"}]
    extra = [
        _event("tool_call", asked, T0),
        _event("tool_result", answered, T0),
        _event("tool_call", planned, T0),
        _event("tool_result", approved, T0),
    ]
    data.events = data.events[:2] + extra + data.events[2:]
    for number, event in enumerate(data.events):
        event["raw_object_key"] = KEY
        event["raw_line_number"] = number + 1
        acting = event["event_type"] in ("response", "thinking", "tool_call")
        event["model"] = "claude-opus-5-5" if acting else None
    data.decisions[0]["evidence_lines"] = [{"key": KEY, "line": 3}]
    return data


def test_each_conversation_shows_thinking_options_decision_and_who_decided() -> None:
    page = render_report_html(_turn_data(), "catalog/x", 0, [])
    assert 'id="conversation-1"' in page and 'id="conversation-2"' in page
    assert "Claude's thinking" in page and "I weigh two options." in page
    assert "Options put to the developer" in page and "small and fast" in page
    assert "Answer: Streaming" in page
    assert "Plan put to the developer for approval" in page
    assert "Decision 1" in page and "Options considered" in page and "stream: low memory" in page
    assert 'class="opt chosen"' in page
    assert "Claude decided · claude-opus-5-5" in page
    assert "reviewer-model" in page


def test_a_decision_sits_under_the_conversation_where_its_evidence_is() -> None:
    turns = build_turns(_turn_data().events, {})
    decisions = [
        {"number": 1, "evidence": [{"transcript_file": KEY, "line": 3}]},
        {"number": 2},
    ]
    placed, loose = assign_decisions(turns, decisions)
    assert [d["number"] for d in placed[1]] == [1]
    assert [d["number"] for d in loose] == [2]


def test_every_template_is_in_the_page_and_nothing_is_loaded_from_the_network() -> None:
    page = render_report_html(_turn_data(), "catalog/x", 0, [])
    for key, _ in THEMES:
        assert f'data-theme-btn="{key}"' in page
    assert "http://" not in page.replace("http://www.w3.org", "")
    assert "https://cdn" not in page and "<script src" not in page


def test_text_from_a_session_cannot_run_as_code_in_the_page() -> None:
    data = _turn_data()
    data.events[0]["content"] = "<script>alert(1)</script> and <img src=x onerror=alert(2)>"
    data.chat["title"] = "T <b>bold</b>"
    page = render_report_html(data, "catalog/x", 0, [])
    assert "<script>alert(1)" not in page and "<img src=x" not in page
    assert "&lt;script&gt;alert(1)" in page
    assert "<title>T &lt;b&gt;bold&lt;/b&gt;</title>" in page


def test_markdown_renders_headings_lists_tables_code_and_stays_safe() -> None:
    text = (
        "# Title\n\n- one\n- **two**\n\n| a | b |\n|---|---|\n| 1 | `x` |\n\n```\n<b>raw</b>\n```\n"
    )
    html = markdown(text)
    assert "<h2>Title</h2>" in html and "<li><strong>two</strong></li>" in html
    assert "<table>" in html and "<code>x</code>" in html
    assert "&lt;b&gt;raw&lt;/b&gt;" in html and "<b>raw</b>" not in html
    assert 'href="javascript:' not in markdown("[x](javascript:alert(1))")
