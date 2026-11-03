"""Processing of a stored EMAIL_REPLY event (C1): fetch the thread, parse it, store it.

Nothing here classifies, posts to Slack, sends, unsubscribes or writes to Smartlead. The only
Smartlead calls are reads. The webhook body is never trusted for the thread itself: it only
tells us which campaign and lead to fetch.

Outcomes:
- first_reply: the thread had no stored lead reply before this event. Stored, ready for C2.
- follow_up:   the lead had already replied. Stored, deliberately not classified.
- failed:      the failure is written on the raw event. A parse failure or bad payload marks the
               event processed with the error; a fetch problem leaves it unprocessed (retryable).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.db.client import (
    fetch_messages,
    find_thread,
    insert_message,
    mark_event_processed,
    record_event_error,
    upsert_thread,
)
from app.db.records import NewMessage, NewThread
from app.ingest.deps import IngestDeps
from app.smartlead.errors import ParseError, SmartleadError
from app.smartlead.models import Direction, Lead, Message
from app.smartlead.thread import parse_thread

logger = logging.getLogger(__name__)

FIRST_REPLY = "first_reply"
FOLLOW_UP = "follow_up"
FAILED = "failed"

# Webhook fields that may carry the Smartlead lead id, tried in this order. Unverified against
# live deliveries, which is why the thread is checked against the lead's email after fetching.
LEAD_ID_FIELDS = ("sl_email_lead_id", "lead_id", "email_lead_id")


class PayloadError(ValueError):
    """The event cannot be processed as delivered (permanent)."""


class FetchProblem(RuntimeError):
    """Smartlead did not give us a usable thread right now (transient)."""


@dataclass(frozen=True)
class Outcome:
    status: str
    thread_id: str | None = None
    detail: str | None = None


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def resolve_lead(smartlead: Any, payload: dict[str, Any]) -> tuple[Lead, dict[str, Any] | None]:
    """The lead to fetch, from the event if it names one, else by searching the master inbox."""
    campaign_id = _to_int(payload.get("campaign_id"))
    if campaign_id is None:
        raise PayloadError("event has no usable campaign_id")
    email = payload.get("to_email")
    email = email.strip() if isinstance(email, str) and email.strip() else None
    for name in LEAD_ID_FIELDS:
        lead_id = _to_int(payload.get(name))
        if lead_id is not None:
            return Lead(campaign_id=campaign_id, lead_id=lead_id, email=email), None
    if email is None:
        raise PayloadError("event has neither a lead id nor to_email")
    item = smartlead.find_inbox_item(campaign_id, email)
    if item is None:
        raise FetchProblem("lead not found in the campaign's master inbox")
    lead_id = _to_int(item.get("email_lead_id"))
    if lead_id is None:
        raise FetchProblem("master-inbox item has no usable email_lead_id")
    return Lead(campaign_id=campaign_id, lead_id=lead_id, email=email), item


def thread_matches_lead(messages: list[Message], email: str | None) -> bool:
    """Guard against an unverified lead id: the lead's email must appear in the thread."""
    if email is None:
        return True
    target = email.strip().lower()
    return any(target in (m.from_addr.strip().lower(), m.to_addr.strip().lower()) for m in messages)


def build_thread(
    lead: Lead,
    item: dict[str, Any] | None,
    payload: dict[str, Any],
    messages: list[Message],
) -> NewThread:
    replies = [m for m in messages if m.direction is Direction.REPLY]
    lead_email = (item or {}).get("lead_email") or lead.email or replies[-1].from_addr
    item = item or {}
    return NewThread(
        email_lead_id=lead.lead_id,
        email_campaign_id=lead.campaign_id,
        lead_email=lead_email,
        email_lead_map_id=_to_int(item.get("email_lead_map_id")),
        campaign_name=item.get("email_campaign_name") or payload.get("campaign_name"),
        lead_first_name=item.get("lead_first_name"),
        lead_last_name=item.get("lead_last_name"),
        smartlead_category_id=_to_int(item.get("lead_category_id")),
        smartlead_lead_status=item.get("lead_status"),
        last_reply_at=replies[-1].timestamp,
    )


def _prior_reply_count(deps: IngestDeps, lead_id: int, campaign_id: int) -> int:
    existing = find_thread(deps.db, lead_id, campaign_id)
    if existing is None:
        return 0
    return sum(1 for m in fetch_messages(deps.db, existing["id"]) if m["direction"] == "REPLY")


def _fail_permanently(deps: IngestDeps, event_id: str, error: str) -> Outcome:
    mark_event_processed(deps.db, event_id, deps.clock(), error=error)
    logger.warning("event %s failed permanently: %s", event_id, error)
    return Outcome(FAILED, detail=error)


def _fail_for_retry(deps: IngestDeps, event_id: str, error: str) -> Outcome:
    record_event_error(deps.db, event_id, error)
    logger.warning("event %s failed, left unprocessed for retry: %s", event_id, error)
    return Outcome(FAILED, detail=error)


def process_reply_event(deps: IngestDeps, event_id: str, payload: dict[str, Any]) -> Outcome:
    try:
        lead, item = resolve_lead(deps.smartlead, payload)
        raw = deps.smartlead.get_thread_raw(lead)
        messages = parse_thread(raw)
    except PayloadError as exc:
        return _fail_permanently(deps, event_id, f"invalid event: {exc}")
    except ParseError as exc:
        return _fail_permanently(deps, event_id, f"thread parse failure: {exc}")
    except (FetchProblem, SmartleadError) as exc:
        return _fail_for_retry(deps, event_id, f"could not fetch the thread: {exc}")

    replies = [m for m in messages if m.direction is Direction.REPLY]
    if not replies:
        return _fail_for_retry(deps, event_id, "fetched thread has no lead reply yet")
    if not thread_matches_lead(messages, lead.email):
        return _fail_for_retry(deps, event_id, "fetched thread does not belong to the event's lead")

    prior_replies = _prior_reply_count(deps, lead.lead_id, lead.campaign_id)
    thread = upsert_thread(deps.db, build_thread(lead, item, payload, messages))
    for message in messages:
        insert_message(deps.db, NewMessage.from_message(thread["id"], message))
    mark_event_processed(deps.db, event_id, deps.clock())

    status = FIRST_REPLY if prior_replies == 0 else FOLLOW_UP
    logger.info(
        "event %s processed: thread %s, %d messages, %s",
        event_id,
        thread["id"],
        len(messages),
        status,
    )
    return Outcome(status, thread_id=thread["id"])
