"""Process one chat: digest, extraction call, independent review call, store.

The two model calls are separate on purpose: the reviewer gets its own instructions and sees only
the digest and the extracted decisions, so it does not simply confirm what the extractor believed.
A decision the reviewer does not answer for is stored as ``needs_review``, never approved.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import pg8000.dbapi

from ccp.decisions import prompts
from ccp.decisions.digest import Digest, build_digest
from ccp.decisions.extraction import (
    AnswerError,
    ExtractedDecision,
    Extraction,
    Review,
    parse_extraction,
    parse_reviews,
)
from ccp.decisions.provider import Provider, ProviderError
from ccp.decisions.repository import (
    DecisionError,
    NewDecision,
    chat_state,
    linked_commits,
    mark_checked,
    save_decisions,
)
from ccp.decisions.settings import DecisionSettings

logger = logging.getLogger("ccp.decisions")

# Outcomes of process_chat.
DONE = "done"
DRY_RUN = "dry_run"
UP_TO_DATE = "up_to_date"
EMPTY = "empty"
NOT_FOUND = "not_found"
NO_DECISIONS = "no_decisions"


@dataclass(frozen=True)
class ChatResult:
    """What happened to one chat (counts only)."""

    session_id: str
    outcome: str
    digest_chars: int = 0
    decisions: int = 0
    approved: int = 0
    needs_review: int = 0
    corrections: int = 0
    dropped: int = 0


def process_chat(
    connection: pg8000.dbapi.Connection,
    provider: Provider | None,
    settings: DecisionSettings,
    session_id: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> ChatResult:
    """Extract, review and store the decisions of one chat.

    Args:
        connection: An open connection.
        provider: The model to ask (may be None with ``dry_run``).
        settings: Model name and limits.
        session_id: The chat.
        force: Process even when the decisions are up to date.
        dry_run: Only build the digest and report its size; call no model, store nothing.

    Raises:
        DecisionError: The model could not be asked, its answer was unusable or storing failed.
    """
    state = chat_state(connection, session_id)
    if state is None:
        return ChatResult(session_id, NOT_FOUND)
    if state.is_empty or not state.has_prompt:
        return ChatResult(session_id, EMPTY)
    if not force and not state.needs_decisions():
        return ChatResult(session_id, UP_TO_DATE)
    digest = build_digest(connection, session_id, settings.max_chars)
    connection.rollback()
    if dry_run:
        return ChatResult(session_id, DRY_RUN, digest_chars=digest.chars)
    if provider is None:
        raise DecisionError("no model provider is available")

    commits = linked_commits(connection, session_id)
    usage: dict[str, Any] = {}
    extraction = _extract(provider, settings, digest, commits)
    _note_usage(usage, "write", provider)
    if not extraction.decisions:
        # Nothing valid came back: keep what is stored rather than wipe it, and remember that the
        # chat was looked at, so it is not sent to the model again until it has new activity.
        mark_checked(connection, session_id)
        return ChatResult(session_id, NO_DECISIONS, digest.chars, dropped=extraction.dropped)
    reviews = _review(provider, settings, digest, extraction)
    _note_usage(usage, "review", provider)
    decisions = [
        _new_decision(item, digest, reviews.get(index))
        for index, item in enumerate(extraction.decisions)
    ]
    corrections = [(digest.refs[item.line].event_id, item.note) for item in extraction.corrections]
    save_decisions(
        connection,
        session_id,
        decisions,
        corrections,
        settings.model,
        prompts.PROMPT_VERSION,
        settings.review_model,
        usage,
    )
    logger.info("decisions of one chat: %s", json.dumps(usage))  # tokens and cost, no chat text
    approved = sum(1 for decision in decisions if decision.status == "approved")
    return ChatResult(
        session_id,
        DONE,
        digest.chars,
        decisions=len(decisions),
        approved=approved,
        needs_review=len(decisions) - approved,
        corrections=len(corrections),
        dropped=extraction.dropped,
    )


def _note_usage(usage: dict[str, Any], step: str, provider: Provider) -> None:
    """Remember what the latest call used (providers that cannot tell leave it out)."""
    used = getattr(provider, "last_usage", None)
    if used is not None:
        usage[step] = used.as_dict()


def _extract(
    provider: Provider, settings: DecisionSettings, digest: Digest, commits: list[str]
) -> Extraction:
    try:
        answer = provider.complete(
            prompts.EXTRACT_SYSTEM, prompts.extract_prompt(digest.text), settings.model
        )
        return parse_extraction(answer, digest.refs, commits)
    except (ProviderError, AnswerError) as error:
        raise DecisionError(f"extraction failed: {error}") from error


def _review(
    provider: Provider, settings: DecisionSettings, digest: Digest, extraction: Extraction
) -> dict[int, Review]:
    asked = [item.as_prompt_dict() for item in extraction.decisions]
    try:
        answer = provider.complete(
            prompts.REVIEW_SYSTEM,
            prompts.review_prompt(digest.text, asked),
            settings.review_model,
        )
        return parse_reviews(answer, len(asked))
    except (ProviderError, AnswerError) as error:
        raise DecisionError(f"review failed: {error}") from error


def _new_decision(item: ExtractedDecision, digest: Digest, review: Review | None) -> NewDecision:
    """Combine a checked decision with its review (no review means a person must look)."""
    if review is None:
        status, note = "needs_review", "the reviewer gave no verdict for this decision"
    else:
        status, note = ("approved" if review.approved else "needs_review"), review.note
    return NewDecision(
        title=item.title,
        goal=item.goal,
        options=item.options,
        chosen=item.chosen,
        reason=item.reason,
        result=item.result,
        tests_summary=item.tests_summary,
        commit_shas=item.commit_shas,
        evidence=[digest.refs[number] for number in item.evidence],
        status=status,
        review_note=note,
        decided_by=item.decided_by,
        files=item.files,
        thinking=item.thinking,
    )
