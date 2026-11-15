"""Key decisions of a chat, extracted and reviewed by an AI, overridable by a person.

For each chat that has messages, :mod:`ccp.decisions.runner` builds a compact digest of the chat
from the index (:mod:`ccp.decisions.digest`), asks a model for the key decisions (goal, options,
what was chosen and why, result, tests, commits, evidence lines), has a second, independent
call review each one, and stores them in the ``decision`` table with who reviewed them. A
person can later approve or reject a decision
(:func:`ccp.decisions.repository.set_decision_status`). See docs/decisions.md.
"""

from ccp.decisions.plugin import register
from ccp.decisions.repository import DecisionError, set_decision_status

__all__ = ["DecisionError", "register", "set_decision_status"]
