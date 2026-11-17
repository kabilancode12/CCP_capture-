"""Checking the model's answers: what is kept and what is dropped."""

import json
from typing import Any

import pytest

from ccp.decisions.digest import LineRef
from ccp.decisions.extraction import AnswerError, parse_extraction, parse_json_object, parse_reviews

REFS = {
    1: LineRef(10, "sessions/c/main/000000000000.jsonl", 1, "prompt"),
    2: LineRef(11, "sessions/c/main/000000000000.jsonl", 2, "reply"),
    3: LineRef(12, "sessions/c/main/000000000000.jsonl", 5, "prompt"),
}
SHA = "a" * 40


def _decision(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Use a queue",
        "goal": "decouple",
        "options": ["queue", "thread"],
        "chosen": "queue",
        "reason": "simple",
        "result": "works",
        "tests_summary": "3 passed",
        "commit_shas": [SHA[:8]],
        "evidence_lines": [1, 2],
    }
    base.update(changes)
    return base


def _parse(*decisions: Any, corrections: Any = None) -> Any:
    answer: dict[str, Any] = {"decisions": list(decisions)}
    if corrections is not None:
        answer["developer_corrections"] = corrections
    return parse_extraction(json.dumps(answer), REFS, [SHA])


def test_a_valid_decision_is_kept_with_its_commit_expanded() -> None:
    result = _parse(_decision())
    [decision] = result.decisions
    assert decision.title == "Use a queue"
    assert decision.commit_shas == [SHA]
    assert decision.evidence == [1, 2]
    assert result.dropped == 0


def test_json_inside_a_code_fence_or_after_chatter_is_read() -> None:
    fenced = "Here you go:\n```json\n" + json.dumps({"decisions": []}) + "\n```"
    assert parse_json_object(fenced) == {"decisions": []}


@pytest.mark.parametrize("text", ["", "no json here", "[1]", '"x"', "{broken"])
def test_text_without_a_json_object_is_an_error(text: str) -> None:
    with pytest.raises(AnswerError):
        parse_json_object(text)


def test_an_answer_without_a_decisions_list_is_an_error() -> None:
    with pytest.raises(AnswerError):
        parse_extraction('{"decisions": "x"}', REFS, [])


@pytest.mark.parametrize(
    "broken",
    [
        _decision(title=""),
        _decision(title=5),
        _decision(chosen="  "),
        _decision(chosen=None),
        "not an object",
        None,
    ],
)
def test_invalid_decisions_are_dropped_and_counted(broken: Any) -> None:
    result = _parse(broken, _decision(title="Good"))
    assert [d.title for d in result.decisions] == ["Good"]
    assert result.dropped == 1


def test_evidence_lines_must_exist_in_the_digest_and_are_unique() -> None:
    result = _parse(_decision(evidence_lines=[1, 1, 99, "2", True, 2.5, 3]))
    assert result.decisions[0].evidence == [1, 3]


def test_commits_that_match_no_linked_commit_are_dropped() -> None:
    result = _parse(_decision(commit_shas=["ffffff1", "zzzzzzz", SHA, SHA[:7].upper()]))
    assert result.decisions[0].commit_shas == [SHA]


def test_missing_optional_fields_become_empty() -> None:
    result = _parse({"title": "T", "chosen": "C"})
    [decision] = result.decisions
    assert (decision.goal, decision.options, decision.reason, decision.tests_summary) == (
        "",
        [],
        "",
        None,
    )
    assert decision.evidence == [] and decision.commit_shas == []


def test_long_text_is_cut() -> None:
    result = _parse(_decision(title="t" * 500, reason="r" * 5000))
    assert len(result.decisions[0].title) == 200
    assert len(result.decisions[0].reason) == 2000


def test_developer_corrections_must_point_at_a_prompt_line() -> None:
    result = _parse(
        corrections=[
            {"line": 3, "note": "wanted tabs"},
            {"line": 3, "note": "duplicate line"},
            {"line": 2, "note": "a reply, not a prompt"},
            {"line": 99, "note": "unknown"},
            {"line": 1, "note": ""},
            {"line": "1", "note": "text line"},
        ]
    )
    assert [(c.line, c.note) for c in result.corrections] == [(3, "wanted tabs")]
    assert result.dropped == 4  # the duplicate is ignored, not counted


def test_a_corrections_value_that_is_not_a_list_is_ignored() -> None:
    assert _parse(corrections="none").corrections == []


def test_reviews_are_matched_by_index() -> None:
    answer = json.dumps(
        {
            "reviews": [
                {"index": 0, "verdict": "approve", "note": "ok"},
                {"index": 1, "verdict": "needs_review", "note": "unsupported"},
                {"index": 1, "verdict": "approve", "note": "second answer ignored"},
                {"index": 5, "verdict": "approve"},
                {"index": 2, "verdict": "maybe"},
                {"index": -1, "verdict": "approve"},
                "junk",
            ]
        }
    )
    reviews = parse_reviews(answer, 3)
    assert sorted(reviews) == [0, 1]
    assert reviews[0].approved and reviews[0].note == "ok"
    assert not reviews[1].approved and reviews[1].note == "unsupported"


def test_a_review_answer_without_a_reviews_list_is_an_error() -> None:
    with pytest.raises(AnswerError):
        parse_reviews('{"reviews": 1}', 1)


ADVICE_REFS = {
    **REFS,
    4: LineRef(13, "sessions/c/main/000000000000.jsonl", 9, "reply", text=(
        "Usually it isn't a good design for a plain  synchronous upload.\nWhere do the files come from?"
    )),
}  # fmt: skip


def _advice(**changes: Any) -> dict[str, Any]:
    return _decision(
        kind="Advice", raised_by="developer asked", outcome="waiting", chosen="",
        short_answer="Works, but not recommended for production at that size",
        use_when=["under 50 MB -> current endpoint", "over 1 GB -> pre-signed URL", 5],
        not_chosen=[{"option": "single request", "why": "proxy limits"}, {"why": "no option"}, 7],
        waiting_for="Where do the files come from?", evidence_lines=[4],
        follows_decision=1, follows_how="Not Followed", **changes,
    )  # fmt: skip


def test_advice_keeps_its_plain_words_and_does_not_need_a_single_choice() -> None:
    answer = parse_extraction(json.dumps({"decisions": [_advice()]}), ADVICE_REFS, [])
    item = answer.decisions[0]
    assert (item.kind, item.raised_by, item.outcome) == ("advice", "developer asked", "waiting")
    assert item.chosen == item.short_answer  # no choice yet: the short answer stands in
    assert item.use_when == ["under 50 MB -> current endpoint", "over 1 GB -> pre-signed URL"]
    assert item.not_chosen == [{"option": "single request", "why": "proxy limits"}]
    assert (item.follows_decision, item.follows_how) == (1, "not followed")


def test_values_outside_the_allowed_words_become_defaults() -> None:
    answer = _parse(_decision(kind="essay", raised_by="nobody", outcome="maybe", follows_how="x"))
    item = answer.decisions[0]
    assert (item.kind, item.raised_by, item.outcome, item.follows_how) == (
        "code change", None, "done", None,
    )  # fmt: skip


def test_a_quote_is_kept_only_when_it_stands_letter_for_letter_in_the_cited_line() -> None:
    quotes = [
        {"line": 4, "text": "Usually it isn't a good design for a plain synchronous upload."},
        {"line": 4, "text": "Where do the files come from?"},
        {"line": 4, "text": "usually it isn't a good design"},  # wrong case: not exact
        {"line": 4, "text": "Never said at all, invented words"},  # not in the line
        {"line": 4, "text": "good"},  # too short to mean anything
        {"line": 3, "text": "Usually it isn't a good design"},  # a line that has no such text
        {"line": 99, "text": "Usually it isn't a good design for a plain"},  # no such line
    ]
    kept = parse_extraction(
        json.dumps({"decisions": [_advice(quotes=quotes)]}), ADVICE_REFS, []
    ).decisions[0].quotes  # fmt: skip
    assert [q["text"] for q in kept] == [
        "Usually it isn't a good design for a plain synchronous upload.",
        "Where do the files come from?",
    ]
    assert kept[0]["who"] == "claude" and kept[0]["line"] == 9
