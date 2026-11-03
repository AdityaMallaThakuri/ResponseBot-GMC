"""The validator's context for a stored thread: label, the lead's first reply, what we sent."""

from app.db.client import Database, Row, fetch_classifications, fetch_messages
from app.templates.facts import Facts
from app.validate.validator import ValidationContext


def validation_context(
    db: Database, thread: Row, facts: Facts, min_confidence: float = 0.0
) -> ValidationContext:
    classification = (fetch_classifications(db, thread["id"]) or [{}])[-1]
    messages = fetch_messages(db, thread["id"])
    replies = [m for m in messages if m["direction"] == "REPLY"]
    sent = [m for m in messages if m["direction"] == "SENT"]
    return ValidationContext(
        label=classification.get("label", ""),
        confidence=float(classification.get("confidence", 0)),
        min_confidence=min_confidence,
        reply=replies[0]["body_clean"] if replies else "",
        outreach=sent[0]["body_clean"] if sent else "",
        campaign_name=str(thread.get("campaign_name") or ""),
        facts=facts,
    )
