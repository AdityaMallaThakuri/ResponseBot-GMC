"""The confidence threshold: low-confidence classifications go to a human with no draft.

The router calls this right after the classifier. Rule-based results carry confidence 1.0,
so they always pass. A missing classification (the classifier raised an error) also goes to a
human, in line with the rule that anything uncertain is handled by a person.
"""

from __future__ import annotations

from app.classify.classifier import Classification

HUMAN_ONLY = "human_only"
CONTINUE = "continue"


def needs_human(classification: Classification | None, min_confidence: float) -> bool:
    """True when the thread must go to human_only: no result, or confidence below the minimum."""
    return classification is None or classification.confidence < min_confidence


def lane_after_threshold(classification: Classification | None, min_confidence: float) -> str:
    """HUMAN_ONLY (no drafted reply) or CONTINUE (the router carries on with the label)."""
    return HUMAN_ONLY if needs_human(classification, min_confidence) else CONTINUE
