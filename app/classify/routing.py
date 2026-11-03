"""Which lane a classified reply goes to (C2). Built on the safety rules in policy.py.

Lanes shown on the Slack card:
- would_suppress: do_not_contact. The only label allowed an automatic action. While sending is
  off the action is only recorded as "would_suppress" and a notice is posted; Smartlead is not
  called.
- human_only: a notice for a person, no draft. Low confidence, a classifier error,
  not_interested (with "suggest: close" at or above its own threshold), out_of_office,
  wrong_person, auto_reply_or_bounce and off_script.
- draft: every other label. Drafting comes in C3, so for now this is a card for a person with
  the Approve / Edit / Skip buttons, which record a decision and do nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.classify.classifier import Classification
from app.classify.confidence import HUMAN_ONLY
from app.classify.policy import AUTO_SUPPRESS, decide

LANE_DRAFT = "draft"
LANE_HUMAN_ONLY = "human_only"
LANE_WOULD_SUPPRESS = "would_suppress"

# Labels that never get a draft, even when the model is confident.
NOTICE_ONLY_LABELS = frozenset(
    {"out_of_office", "wrong_person", "auto_reply_or_bounce", "off_script"}
)


# Labels a reply may be drafted for (the draft lane). Every other label goes to a person.
DRAFT_LABELS = frozenset(
    {
        "meeting_request",
        "info_request",
        "free_list_request",
        "price_question",
        "price_and_info",
        "lead_magnet_request",
        "trial_request",
        "booked_confirmation",
        "objection_info",
        "objection_price",
    }
)


@dataclass(frozen=True)
class Route:
    lane: str
    reason: str  # why, in a few words, for the card
    note: str | None = None  # for example "suggest: close"
    buttons: bool = False  # Approve / Edit / Skip only on the draft lane
    would_suppress: bool = False  # record a would_suppress action


def route(
    classification: Classification | None,
    min_confidence: float,
    min_confidence_not_interested: float,
) -> Route:
    """None means the classifier failed."""
    if classification is None:
        return Route(LANE_HUMAN_ONLY, "classifier error")
    decision = decide(classification, min_confidence, min_confidence_not_interested)
    if decision.auto_action == AUTO_SUPPRESS:
        return Route(
            LANE_WOULD_SUPPRESS, "do_not_contact: recorded only, nothing sent", would_suppress=True
        )
    if decision.lane == HUMAN_ONLY:
        return Route(LANE_HUMAN_ONLY, decision.reason, note=decision.note)
    if classification.label in NOTICE_ONLY_LABELS:
        return Route(LANE_HUMAN_ONLY, f"{classification.label}: no draft")
    return Route(LANE_DRAFT, "drafting is not built yet", buttons=True)
