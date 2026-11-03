"""What the router may do with a classification: the safety rules in one place.

Rules, in order:
1. No classification (the classifier raised an error) -> human_only, no draft.
2. Confidence below CLASSIFIER_MIN_CONFIDENCE -> human_only, no draft.
3. Only do_not_contact may trigger an automatic suppression or unsubscribe. Nothing else is
   ever auto-suppressed or auto-closed.
4. not_interested never closes automatically. Below CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED
   it goes to human_only; at or above it, it still goes to a human, with a "suggest: close"
   note. No automatic action in either case.
5. Everything else continues down the router (drafts still need a human to approve them).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.classify.classifier import Classification
from app.classify.confidence import CONTINUE, HUMAN_ONLY, needs_human

AUTO_SUPPRESS = "suppress"
AUTO_ACTION_LABELS = frozenset({"do_not_contact"})
NEVER_AUTO_CLOSE = frozenset({"not_interested", "objection_info", "objection_price"})
SUGGEST_CLOSE = "suggest: close"


class AutoActionNotAllowed(ValueError):
    """Raised when code tries to suppress or unsubscribe on a label that may not trigger it."""


@dataclass(frozen=True)
class Decision:
    lane: str  # HUMAN_ONLY (no draft) or CONTINUE
    auto_action: str | None  # AUTO_SUPPRESS for do_not_contact only, otherwise None
    note: str | None  # shown to the human, for example "suggest: close"
    reason: str


def may_auto_suppress(label: str) -> bool:
    return label in AUTO_ACTION_LABELS


def require_auto_action_allowed(label: str) -> None:
    """Call before any automatic suppression or unsubscribe. Raises for any other label."""
    if not may_auto_suppress(label):
        raise AutoActionNotAllowed(f"{label} may not trigger an automatic suppression")


def decide(
    classification: Classification | None,
    min_confidence: float,
    min_confidence_not_interested: float,
) -> Decision:
    if classification is None:
        return Decision(HUMAN_ONLY, None, None, "no classification")
    if needs_human(classification, min_confidence):
        return Decision(HUMAN_ONLY, None, None, f"confidence below {min_confidence}")

    label = classification.label
    if label == "not_interested":
        if classification.confidence < min_confidence_not_interested:
            reason = f"not_interested below {min_confidence_not_interested}"
            return Decision(HUMAN_ONLY, None, None, reason)
        return Decision(HUMAN_ONLY, None, SUGGEST_CLOSE, "not_interested: a human closes it")
    if may_auto_suppress(label):
        return Decision(CONTINUE, AUTO_SUPPRESS, None, "do_not_contact")
    return Decision(CONTINUE, None, None, label)
