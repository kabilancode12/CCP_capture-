"""The designed web page: thinking, options and decisions per conversation; safe and self-contained."""

from test_catalog_report import KEY, T0, _data, _event

from ccp.catalog.loader import ChatData
from ccp.catalog.report_html import (
    THEMES,
    assign_decisions,
    build_turns,
    markdown,
    render_report_html,
)


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


def test_the_chosen_option_is_the_one_the_decision_names_not_one_it_only_rejects() -> None:
    from ccp.catalog.report_html import chosen_option

    options = [
        "CsvHelper: robust but API mismatch in v34",
        "hand-written RFC 4180 parser: no dependency, own maintenance",
    ]
    chosen = "Replace CsvHelper with a small custom streaming RFC 4180 parser using a StreamReader buffer."
    assert chosen_option(options, chosen) == 1
    assert chosen_option(options, "Keep CsvHelper as the parser") == 0
    assert chosen_option(options, "Something unrelated entirely") is None
    assert chosen_option([], "anything") is None


def test_a_decision_shows_the_prompt_the_developer_gave_at_that_moment() -> None:
    data = _turn_data()
    data.decisions[0]["prompt_text"] = "Please add a retry to the upload step"
    data.decisions[0]["prompt_at"] = T0
    data.decisions[0]["prompt_uuid"] = "u-1"
    page = render_report_html(data, "catalog/x", 0, [])
    assert "The developer asked" in page and "Please add a retry to the upload step" in page
    from ccp.catalog import documents
    from ccp.catalog.report import render_report

    assert documents.build_decisions(data)[0]["prompt"] == {
        "text": "Please add a retry to the upload step", "at": T0, "entry": "u-1",
    }  # fmt: skip
    assert "Please add a retry to the upload step" in render_report(data, "catalog/x", 0, [])


def test_a_line_that_looks_like_a_table_row_but_is_not_one_does_not_hang_the_renderer() -> None:
    text = "intro\n| just a pipe, no separator row\nnext line\n\n|another\n> quote\n- item\n"
    html = markdown(text)
    assert "just a pipe, no separator row" in html and "another" in html
    assert "<blockquote>" in html and "<li>item</li>" in html


def test_a_decision_without_code_shows_its_short_answer_conditions_and_links() -> None:
    page = render_report_html(_turn_data(), "catalog/x", 0, [])
    assert "Short answer" in page and "Works, but not recommended at that size" in page
    assert "When to use which" in page and "under 50 MB -&gt; current endpoint" in page
    assert "Not chosen, and why" in page and "proxy limits" in page
    assert "Claude is waiting for" in page and "where the files come from" in page
    assert "Follows earlier advice:" in page and "Earlier advice" in page
    assert "Exact words" in page and "not a good design" in page
    assert 'class="chip kind"' in page and 'class="chip out-followed"' in page


def test_every_model_that_worked_on_a_decision_is_shown_with_its_effort_level() -> None:
    page = render_report_html(_turn_data(), "catalog/x", 0, [])
    assert "claude-opus-5-5 · high" in page and "claude-sonnet-5-5" in page
