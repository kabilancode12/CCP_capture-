"""Reading and checking the model's answers (no database, no model call).

The model's text is never trusted: it is parsed as JSON, every field is checked, and an item that is
not valid is dropped (the rest is kept). Evidence lines must be numbers that exist in the digest.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from ccp.decisions.digest import FileRef, LineRef

MAX_TITLE = 200
MAX_TEXT = 2000
MAX_NOTE = 300
MAX_OPTIONS = 10
MAX_EVIDENCE = 20
MAX_REVIEW_NOTE = 300
MAX_FILES = 60
MAX_THINKING = 5
MAX_QUOTES = 6
MAX_QUOTE = 400
MIN_QUOTE = 12

DECIDED_BY = ("developer", "claude")
KINDS = ("code change", "advice", "bug cause", "plan")
RAISED_BY = ("developer asked", "claude suggested", "claude disagreed")
# What a decision without code can be: still waiting for the developer, postponed, or settled.
OWN_OUTCOMES = ("waiting", "postponed", "done")
# What a later decision says about earlier advice.
FOLLOW_HOW = ("followed", "not followed", "changed later")

_SHA = re.compile(r"[0-9a-f]{7,40}")

APPROVE = "approve"
NEEDS_REVIEW = "needs_review"


class AnswerError(ValueError):
    """The model's answer is not usable. The message names the problem, never the content."""


@dataclass(frozen=True)
class ExtractedDecision:
    """One checked decision, before it is reviewed."""

    title: str
    goal: str
    options: list[str]
    chosen: str
    reason: str
    result: str
    tests_summary: str | None
    commit_shas: list[str]
    evidence: list[int]  # digest line numbers, all present in the digest
    decided_by: str | None = None  # developer | claude
    files: list[FileRef] = field(default_factory=list)  # read from the recorded data
    thinking: list[LineRef] = field(default_factory=list)  # Claude's thinking notes, as recorded
    kind: str = "code change"
    raised_by: str | None = None
    outcome: str = "done"  # waiting | postponed | done (code adds followed / not followed ...)
    short_answer: str = ""
    use_when: list[str] = field(default_factory=list)
    not_chosen: list[dict[str, str]] = field(default_factory=list)  # [{"option", "why"}]
    waiting_for: str = ""
    quotes: list[dict[str, Any]] = field(default_factory=list)  # exact words, checked
    follows_decision: int | None = None  # 1-based number of an earlier decision of this answer
    follows_open: int | None = None  # number in the OPEN ADVICE list
    follows_how: str | None = None  # followed | not followed | changed later

    def as_prompt_dict(self) -> dict[str, Any]:
        """The decision as the reviewer sees it."""
        return {
            "title": self.title,
            "goal": self.goal,
            "options": self.options,
            "chosen": self.chosen,
            "reason": self.reason,
            "result": self.result,
            "tests": self.tests_summary,
            "decided_by": self.decided_by,
            "files_changed": [
                {"path": item.path, "change": item.change, "via": item.via} for item in self.files
            ],
            "thinking_notes": [ref.note for ref in self.thinking],
            "commit_shas": self.commit_shas,
            "evidence_lines": self.evidence,
            "kind": self.kind,
            "raised_by": self.raised_by,
            "outcome": self.outcome,
            "short_answer": self.short_answer,
            "use_when": self.use_when,
            "not_chosen": self.not_chosen,
            "waiting_for": self.waiting_for,
            "quotes": [{"line": q["digest_line"], "text": q["text"]} for q in self.quotes],
            "follows_decision": self.follows_decision,
            "follows_open_advice": self.follows_open,
            "follows_how": self.follows_how,
        }


@dataclass(frozen=True)
class DeveloperCorrection:
    """A prompt where the developer corrected Claude: the digest line and a short note."""

    line: int
    note: str


@dataclass(frozen=True)
class Extraction:
    """What the extraction call produced, after checking."""

    decisions: list[ExtractedDecision] = field(default_factory=list)
    corrections: list[DeveloperCorrection] = field(default_factory=list)
    dropped: int = 0  # items that were not valid


@dataclass(frozen=True)
class Review:
    """The reviewer's verdict on one decision."""

    approved: bool
    note: str


def parse_json_object(text: str) -> dict[str, Any]:
    """Read the JSON object in a model's answer (it may sit inside a Markdown code fence).

    Raises:
        AnswerError: There is no JSON object in the text.
    """
    stripped = text.strip()
    candidates = [stripped]
    first, last = stripped.find("{"), stripped.rfind("}")
    if 0 <= first < last:
        candidates.append(stripped[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AnswerError("the answer is not a JSON object")


def parse_extraction(text: str, refs: dict[int, LineRef], known_commits: list[str]) -> Extraction:
    """Check the extraction answer.

    Args:
        text: The model's answer.
        refs: The digest's line numbers (only these may be cited).
        known_commits: Full ids of the commits linked to the chat; a cited id that starts one
            of them is replaced by the full id, one that matches none is dropped.

    Raises:
        AnswerError: The answer is not a JSON object with a ``decisions`` list.
    """
    data = parse_json_object(text)
    raw_decisions = data.get("decisions")
    if not isinstance(raw_decisions, list):
        raise AnswerError("the answer has no decisions list")
    decisions, dropped = [], 0
    for item in raw_decisions:
        decision = _decision(item, refs, known_commits)
        if decision is None:
            dropped += 1
        else:
            decisions.append(decision)
    corrections: list[DeveloperCorrection] = []
    raw_corrections = data.get("developer_corrections", [])
    for item in raw_corrections if isinstance(raw_corrections, list) else []:
        correction = _correction(item, refs)
        if correction is None:
            dropped += 1
        elif all(known.line != correction.line for known in corrections):
            corrections.append(correction)
    return Extraction(decisions, corrections, dropped)


def parse_reviews(text: str, count: int) -> dict[int, Review]:
    """Check the review answer: a verdict per decision index (missing ones are absent).

    Raises:
        AnswerError: The answer is not a JSON object with a ``reviews`` list.
    """
    data = parse_json_object(text)
    raw = data.get("reviews")
    if not isinstance(raw, list):
        raise AnswerError("the answer has no reviews list")
    reviews: dict[int, Review] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        index, verdict = item.get("index"), item.get("verdict")
        if not _is_int(index) or not 0 <= index < count or index in reviews:
            continue
        if verdict not in (APPROVE, NEEDS_REVIEW):
            continue
        note = _text(item.get("note"), MAX_REVIEW_NOTE) or ""
        reviews[index] = Review(approved=verdict == APPROVE, note=note)
    return reviews


def _decision(
    item: Any, refs: dict[int, LineRef], known_commits: list[str]
) -> ExtractedDecision | None:
    if not isinstance(item, dict):
        return None
    title = _text(item.get("title"), MAX_TITLE)
    chosen = _text(item.get("chosen"), MAX_TEXT) or _text(item.get("short_answer"), MAX_TEXT)
    if not title or not chosen:
        return None
    evidence = [n for n in _ints(item.get("evidence_lines")) if n in refs]
    evidence = list(dict.fromkeys(evidence))[:MAX_EVIDENCE]
    follows_how = _choice(item.get("follows_how"), FOLLOW_HOW)
    follows_decision = item.get("follows_decision")
    follows_open = item.get("follows_open_advice")
    return ExtractedDecision(
        title=title,
        goal=_text(item.get("goal"), MAX_TEXT) or "",
        options=_strings(item.get("options"), MAX_OPTIONS),
        chosen=chosen,
        reason=_text(item.get("reason"), MAX_TEXT) or "",
        result=_text(item.get("result"), MAX_TEXT) or "",
        tests_summary=_text(item.get("tests", item.get("tests_summary")), MAX_TEXT),
        commit_shas=_commits(item.get("commit_shas"), known_commits),
        evidence=evidence,
        decided_by=_decided_by(item.get("decided_by")),
        files=_files(item.get("file_lines"), refs),
        thinking=_thinking(item.get("thinking_lines"), refs),
        kind=_choice(item.get("kind"), KINDS) or "code change",
        raised_by=_choice(item.get("raised_by"), RAISED_BY),
        outcome=_choice(item.get("outcome"), OWN_OUTCOMES) or "done",
        short_answer=_text(item.get("short_answer"), MAX_TEXT) or "",
        use_when=_strings(item.get("use_when"), MAX_OPTIONS),
        not_chosen=_not_chosen(item.get("not_chosen")),
        waiting_for=_text(item.get("waiting_for"), MAX_TEXT) or "",
        quotes=_quotes(item.get("quotes"), refs),
        follows_decision=follows_decision if _is_int(follows_decision) else None,
        follows_open=follows_open if _is_int(follows_open) else None,
        follows_how=follows_how,
    )


def _choice(value: Any, allowed: tuple[str, ...]) -> str | None:
    """The value when it is one of the allowed words (case and spaces are not important)."""
    text = " ".join(value.lower().split()) if isinstance(value, str) else ""
    return text if text in allowed else None


def _not_chosen(value: Any) -> list[dict[str, str]]:
    """The options that were not chosen, each with the reason: ``[{"option", "why"}]``."""
    found: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        option, why = _text(item.get("option"), MAX_NOTE), _text(item.get("why"), MAX_NOTE)
        if option:
            found.append({"option": option, "why": why or "not stated"})
    return found[:MAX_OPTIONS]


def _squash(text: str) -> str:
    """White space made single, to compare a quote with the recorded text."""
    return " ".join(text.split())


def _quotes(value: Any, refs: dict[int, LineRef]) -> list[dict[str, Any]]:
    """Exact words: kept only when they really stand in the cited prompt or reply.

    The model cites a digest line and the words; the words are checked against the recorded text of
    that line, and what is kept is the recorded text, so a paraphrase or an invention is dropped.
    """
    found: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        number, words = item.get("line"), _text(item.get("text"), MAX_QUOTE)
        ref = refs.get(number) if _is_int(number) else None
        if ref is None or words is None or ref.kind not in ("prompt", "reply"):
            continue
        words = _squash(words)
        if len(words) < MIN_QUOTE or words not in _squash(ref.text):
            continue  # what is kept is a piece of the recorded text, letter for letter
        entry = {
            "digest_line": number,
            "key": ref.raw_object_key,
            "line": ref.raw_line_number,
            "who": "developer" if ref.kind == "prompt" else "claude",
            "text": words,
        }
        if all(entry["text"] != known["text"] for known in found):
            found.append(entry)
    return found[:MAX_QUOTES]


def _decided_by(value: Any) -> str | None:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if text in DECIDED_BY else None


def _files(value: Any, refs: dict[int, LineRef]) -> list[FileRef]:
    """The files of the cited EDIT and SHELL lines: names come from the data, one entry per file."""
    found: dict[str, FileRef] = {}
    for number in _ints(value):
        ref = refs.get(number)
        if ref is None:
            continue
        for item in ref.files:
            found[item.path] = item  # a later change of the same file replaces the earlier one
    return list(found.values())[:MAX_FILES]


def _thinking(value: Any, refs: dict[int, LineRef]) -> list[LineRef]:
    """The cited THINK lines: the notes come from the data, at most a few, each once."""
    cited = [refs[n] for n in _ints(value) if n in refs and refs[n].kind == "think"]
    unique = {(ref.raw_object_key, ref.raw_line_number): ref for ref in cited}
    return list(unique.values())[:MAX_THINKING]


def _correction(item: Any, refs: dict[int, LineRef]) -> DeveloperCorrection | None:
    if not isinstance(item, dict):
        return None
    line, note = item.get("line"), _text(item.get("note"), MAX_NOTE)
    if not _is_int(line) or line not in refs or refs[line].kind != "prompt" or not note:
        return None
    return DeveloperCorrection(line, note)


def _commits(value: Any, known: list[str]) -> list[str]:
    found: list[str] = []
    for sha in _strings(value, MAX_OPTIONS):
        lowered = sha.lower()
        if not _SHA.fullmatch(lowered):
            continue
        full = next((k for k in known if k.lower().startswith(lowered)), None)
        if full is not None and full not in found:
            found.append(full)
    return found


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _ints(value: Any) -> list[int]:
    return [item for item in value if _is_int(item)] if isinstance(value, list) else []


def _strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    texts = (_text(item, MAX_TEXT) for item in value)
    return [text for text in texts if text][:limit]


def _text(value: Any, limit: int) -> str | None:
    """A non-empty string cut to ``limit`` characters, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    return stripped if len(stripped) <= limit else stripped[: limit - 3] + "..."
