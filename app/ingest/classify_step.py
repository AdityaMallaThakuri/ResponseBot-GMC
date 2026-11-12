"""C2: classify a thread's first prospect reply and post it to Slack.

Runs inside the webhook's background task, after C1 has stored the thread and messages.
It never sends, unsubscribes or writes to Smartlead, and it makes no Smartlead call at all. For a
do_not_contact reply it records a "would_suppress" action (and nothing else) and posts a notice.

Idempotent, so a retried event cannot post twice: a thread that already has a Slack card is
left alone (this is also what keeps follow-up replies unclassified), a reply that already has a
classification is not classified again, and the would_suppress action is recorded once.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.classify.classifier import Classification
from app.classify.routing import LANE_DRAFT, LANE_WOULD_SUPPRESS, Route, route
from app.db.client import (
    Row,
    fetch_actions,
    fetch_classifications,
    fetch_messages,
    fetch_slack_cards,
    get_thread,
    insert_action,
    insert_classification,
    insert_slack_card,
)
from app.db.records import (
    ActionType,
    CardLane,
    CardStatus,
    NewAction,
    NewClassification,
    NewSlackCard,
)
from app.ingest.deps import IngestDeps
from app.ingest.draft_step import DraftView, draft_enabled, prepare_draft
from app.slack.card import ReplyCard, post_reply_card
from app.smartlead.models import Direction, Message

logger = logging.getLogger(__name__)


class StepError(RuntimeError):
    """A problem with a safe message (no email text) that should be written on the event."""


def _lead_name(thread: Row, payload: dict[str, Any]) -> str:
    name = " ".join(p for p in (thread.get("lead_first_name"), thread.get("lead_last_name")) if p)
    return name or str(payload.get("to_name") or "").strip() or thread["lead_email"]


def _stored_classification(row: Row) -> Classification:
    rule_hit = row.get("rule_hit")
    return Classification(
        row["label"],
        float(row["confidence"]),
        row["reason"],
        "rule" if rule_hit else "llm",
        rule_hit=rule_hit,
    )


def _classify(
    deps: IngestDeps, reply: Message, campaign: str, outreach: str
) -> Classification | None:
    """The classification, or None if the classifier failed (a human then gets a notice)."""
    try:
        return deps.classifier.classify(reply.text, campaign, outreach)
    except Exception as exc:  # noqa: BLE001 - any failure means "send to a human", never a guess
        logger.error("classifier failed: %s", type(exc).__name__)
        return None


def _store_classification(
    deps: IngestDeps, thread: Row, reply_row: Row, result: Classification
) -> Row:
    return insert_classification(
        deps.db,
        NewClassification(
            thread_id=thread["id"],
            reply_message_id=reply_row["id"],
            label=result.label,
            confidence=result.confidence,
            reason=result.reason,
            model="rules" if result.source == "rule" else deps.classifier.model,
            prompt_version=deps.classifier.version,
            smartlead_category_id=thread.get("smartlead_category_id"),
            rule_hit=result.rule_hit,
        ),
    )


def _record_would_suppress(deps: IngestDeps, thread_id: str, result: Classification) -> None:
    if any(a["type"] == ActionType.WOULD_SUPPRESS.value for a in fetch_actions(deps.db, thread_id)):
        return
    insert_action(
        deps.db,
        NewAction(
            thread_id=thread_id,
            type=ActionType.WOULD_SUPPRESS,
            triggered_by="bot",
            reversible=False,
            details={
                "label": result.label,
                "confidence": result.confidence,
                "source": result.source,
                "rule_hit": result.rule_hit,
                "scope": "campaign",
                "note": "recorded only: nothing was sent to Smartlead",
            },
        ),
    )


def _card(
    thread: Row,
    payload: dict[str, Any],
    reply: Message,
    result: Classification | None,
    plan: Route,
    view: DraftView | None = None,
) -> ReplyCard:
    return ReplyCard(
        thread_ref=thread["id"],
        lead_name=_lead_name(thread, payload),
        company=thread.get("company"),
        campaign=thread.get("campaign_name") or str(payload.get("campaign_name") or "(unknown)"),
        reply=reply.text,
        lane=plan.lane,
        label=result.label if result else None,
        confidence=result.confidence if result else None,
        reason=result.reason if result else plan.reason,
        note=plan.note,
        draft_text=view.text if view else None,
        draft_flags=view.flags if view else (),
        draft_notes=view.notes if view else (),
    )


def handle_first_reply(
    deps: IngestDeps, thread: Row, stored: list[tuple[Message, Row]], payload: dict[str, Any]
) -> bool:
    """Classify the thread's first reply and post its card. True if a card was posted now."""
    if fetch_slack_cards(deps.db, thread["id"]):
        return False  # already handled: a follow-up reply, or a retried event

    replies = [(m, row) for m, row in stored if m.direction is Direction.REPLY]
    first, first_row = replies[0]  # stored oldest first: the first prospect reply
    outreach = next(
        (
            m.text
            for m, _ in stored
            if m.direction is Direction.SENT and m.timestamp <= first.timestamp
        ),
        "",
    )
    campaign = thread.get("campaign_name") or str(payload.get("campaign_name") or "(unknown)")

    done = [
        c
        for c in fetch_classifications(deps.db, thread["id"])
        if c["reply_message_id"] == first_row["id"]
    ]
    classification_id: str | None = None
    if done:
        result: Classification | None = _stored_classification(done[-1])
        classification_id = done[-1]["id"]
    else:
        result = _classify(deps, first, campaign, outreach)
        if result is not None:
            classification_id = _store_classification(deps, thread, first_row, result)["id"]

    cfg = deps.config
    plan = route(result, cfg.min_confidence, cfg.min_confidence_not_interested)
    if plan.would_suppress and result is not None:
        _record_would_suppress(deps, thread["id"], result)

    view: DraftView | None = None
    draft_id: str | None = None
    if plan.lane == LANE_DRAFT and draft_enabled(deps) and result and classification_id:
        plan, view, draft_id = prepare_draft(
            deps, thread, classification_id, result, first, outreach, plan
        )

    channel = cfg.channel_for(plan.lane)
    if not channel:
        raise StepError(
            "no Slack channel configured: set SLACK_DRAFT_CHANNEL (SLACK_ALERT_CHANNEL is optional)"
        )
    ts, channel_id = post_reply_card(
        deps.slack, channel, _card(thread, payload, first, result, plan, view)
    )
    if plan.lane == LANE_WOULD_SUPPRESS:
        db_lane = CardLane.AUTO_ACTION
    elif plan.lane == LANE_DRAFT and draft_id:
        db_lane = CardLane.DRAFT  # the table requires a draft_id for lane 'draft'
    else:
        db_lane = CardLane.HUMAN_ONLY  # including a draft lane card with no draft (C2 behaviour)
    insert_slack_card(
        deps.db,
        NewSlackCard(
            thread_id=thread["id"],
            lane=db_lane,
            draft_id=draft_id,
            channel=channel_id,
            slack_ts=ts,
            status=CardStatus.POSTED,
        ),
    )
    logger.info("thread %s classified and posted: lane %s", thread["id"], plan.lane)
    return True


def messages_from_rows(rows: list[Row]) -> list[tuple[Message, Row]]:
    """Stored message rows back into parsed messages, oldest first, each with its row."""
    pairs = [
        (
            Message(
                direction=Direction(row["direction"]),
                timestamp=datetime.fromisoformat(str(row["sent_at"]).replace("Z", "+00:00")),
                text=row["body_clean"],
                stats_id=row["stats_id"],
                message_id=row["message_id"],
                from_addr=row["from_address"],
                to_addr=row["to_address"],
            ),
            row,
        )
        for row in rows
    ]
    return sorted(pairs, key=lambda pair: pair[0].timestamp)


def classify_stored_thread(deps: IngestDeps, thread_id: str) -> bool:
    """Run the C2 step from rows already in the database. Makes no Smartlead call.

    For a thread whose event was stored before classification existed, or whose C2 step failed.
    True if a card was posted now; False if the thread already has one.
    """
    thread = get_thread(deps.db, thread_id)
    if thread is None:
        raise StepError(f"thread {thread_id} not found")
    stored = messages_from_rows(fetch_messages(deps.db, thread_id))
    if not any(m.direction is Direction.REPLY for m, _ in stored):
        raise StepError(f"thread {thread_id} has no stored lead reply")
    return handle_first_reply(deps, thread, stored, {})
