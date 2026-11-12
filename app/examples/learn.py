"""The learning loop: turn replies Ray sent into new examples.

Every LEARN_INTERVAL_MINUTES (default 30) `learn_once` looks at the drafts that were posted on a
Slack card and have not been answered yet. For each it fetches the thread from Smartlead (READ
ONLY: this module has no way to send, categorise or change anything). If a SENT message now exists
after the prospect's first reply, that message is saved as a new example (source "ray_sent") with a
0 to 100 similarity score between our draft and what was sent. The next draft already uses it.

Drafts older than MAX_AGE_DAYS are no longer checked. A thread that cannot be fetched or parsed is
skipped and tried again on the next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db.client import (
    fetch_classifications,
    fetch_proposed_drafts,
    find_example_for_draft,
    get_thread,
    insert_example,
)
from app.db.records import ExampleSource, NewExample
from app.examples.library import parse_time
from app.examples.similarity import similarity
from app.smartlead.errors import ParseError, SmartleadError
from app.smartlead.models import Direction, Lead, Message
from app.smartlead.thread import parse_thread

logger = logging.getLogger(__name__)

MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class LearnResult:
    checked: int = 0
    learned: int = 0
    waiting: int = 0
    errors: int = 0


def sent_after_first_reply(messages: list[Message]) -> tuple[Message, Message] | None:
    """(the prospect's first reply, the first SENT message after it), or None."""
    replies = sorted((m for m in messages if m.direction is Direction.REPLY), key=_when)
    if not replies:
        return None
    first = replies[0]
    later = sorted(
        (m for m in messages if m.direction is Direction.SENT and m.timestamp > first.timestamp),
        key=_when,
    )
    return (first, later[0]) if later else None


def _when(message: Message) -> datetime:
    return message.timestamp


def learn_from_draft(deps: Any, draft: dict[str, Any]) -> bool:
    """True if a new example was saved for this draft. Raises on a fetch or parse problem."""
    thread = get_thread(deps.db, draft["thread_id"])
    if thread is None:
        return False
    lead = Lead(
        campaign_id=int(thread["email_campaign_id"]),
        lead_id=int(thread["email_lead_id"]),
        email=thread.get("lead_email"),
    )
    messages = parse_thread(deps.smartlead.get_thread_raw(lead))
    found = sent_after_first_reply(messages)
    if found is None:
        return False
    reply, sent = found
    classifications = fetch_classifications(deps.db, thread["id"])
    label = (classifications[-1] if classifications else {}).get("label")
    if not label or not sent.text.strip():
        return False
    shown = draft.get("final_text") or draft["text"]
    insert_example(
        deps.db,
        NewExample(
            source=ExampleSource.RAY_SENT,
            source_thread_id=thread["id"],
            label=label,
            prospect_reply=reply.text,
            gamic_reply=sent.text,
            campaign_name=str(thread.get("campaign_name") or ""),
            replied_at=reply.timestamp,
            thread_id=thread["id"],
            draft_id=draft["id"],
            draft_similarity=similarity(shown, sent.text),
        ),
    )
    return True


def learn_once(deps: Any, now: datetime | None = None) -> LearnResult:
    """One pass over the drafts still waiting for Ray's reply. Never raises for one bad thread."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    checked = learned = waiting = errors = 0
    for draft in fetch_proposed_drafts(deps.db):
        created = parse_time(draft.get("created_at"))
        if created is not None and created < cutoff:
            continue
        if find_example_for_draft(deps.db, draft["id"]) is not None:
            continue
        checked += 1
        try:
            if learn_from_draft(deps, draft):
                learned += 1
            else:
                waiting += 1
        except (SmartleadError, ParseError) as exc:
            errors += 1
            logger.warning(
                "learning loop: could not read thread for draft %s: %s", draft["id"], exc
            )
        except Exception as exc:  # noqa: BLE001 - one bad draft must not stop the others
            errors += 1
            logger.error("learning loop: draft %s failed: %s", draft["id"], type(exc).__name__)
    logger.info(
        "learning loop: %d checked, %d learned, %d waiting, %d errors",
        checked,
        learned,
        waiting,
        errors,
    )
    return LearnResult(checked, learned, waiting, errors)
