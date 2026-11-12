"""Sync examples from Smartlead (read only): the replies Ray really sent to positive leads.

For every watched GMC campaign (and its POS REPLY children) the master inbox is read, five
campaigns per call, with message history. Only leads in a positive or interesting category are
used (`SYNC_CATEGORIES`, matched on the inbox item's `lead_category_id` here in code, so no
unverified Smartlead filter is needed). For each such thread the pair is:

- the prospect's first reply, and
- OUR next SENT message that is not a sequence step (the learning loop's matching: a message that
  is 80+ similar to one of the campaign's steps is an automatic follow-up and is skipped).

The prospect reply is classified with our classifier and the pair becomes a `smartlead_sync`
example, dated by when our reply was sent. A thread is skipped if the library already has its lead
id, or already has a reply that is 90+ similar to ours. Nothing here sends, categorises or changes
anything in Smartlead; the only write is to `reply_examples` (and only when `write` is true).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db.records import ExampleSource, NewExample
from app.examples.learn import sent_after_first_reply, step_texts
from app.examples.similarity import similarity
from app.smartlead.errors import ParseError, SmartleadError
from app.smartlead.inbox import inbox_items
from app.smartlead.models import Direction
from app.smartlead.thread import parse_thread

logger = logging.getLogger(__name__)

# Smartlead lead categories whose threads are used (ids from app/config/smartlead_categories.yaml).
SYNC_CATEGORIES: dict[int, str] = {
    1: "Interested",
    2: "Meeting Request",
    5: "Information Request",
    1589: "requested pricing",
    96272: "Booked",
    118216: "Lead Magnet Follow Up",
    113794: "Generate Lead magnet",
    126961: "Generate Accounting Lead Magnet",
    131581: "Generater SAAS Lead Magnet",
    131680: "Generate SEO Lead Magnet",
    134584: "Generate BSM PI Lead Magnet",
    144352: "Generate M&A Lead Magnet",
    170257: "Generate Rework Lead Magnet",
}
# Labels that are never an example of how to answer a lead.
SKIP_LABELS = frozenset(
    {"do_not_contact", "out_of_office", "auto_reply_or_bounce", "wrong_person", "not_interested"}
)
ALREADY_HAVE = 90.0  # similarity from which a reply counts as already in the library
BATCH = 5  # campaigns per master-inbox call (Smartlead's maximum)
PAGE = 20  # inbox items per page (Smartlead's maximum)


@dataclass
class SyncResult:
    pairs: list[NewExample] = field(default_factory=list)
    inbox_threads: int = 0
    in_categories: int = 0
    already_in_library: int = 0
    no_answer: int = 0  # no SENT message after the reply that is not a sequence step
    sequence_unavailable: int = 0
    skipped_label: int = 0
    low_confidence: int = 0
    errors: int = 0
    stored: int = 0  # set by the caller after writing

    @property
    def by_label(self) -> Counter[str]:
        return Counter(p.label for p in self.pairs)

    @property
    def by_campaign(self) -> Counter[str]:
        return Counter(p.campaign_name for p in self.pairs)

    def date_range(self) -> tuple[datetime, datetime] | None:
        dates = [p.sent_at for p in self.pairs if p.sent_at]
        return (min(dates), max(dates)) if dates else None


def _first_sent_text(messages: list[Any]) -> str:
    sent = sorted((m for m in messages if m.direction is Direction.SENT), key=lambda m: m.timestamp)
    return sent[0].text if sent else ""


def sync_examples(
    smartlead: Any,
    classifier: Any,
    campaigns: list[Any],
    known_ids: set[str],
    known_replies: list[str],
    *,
    min_confidence: float = 0.75,
    max_threads: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> SyncResult:
    """Read the inbox of every campaign and return the new pairs. Never writes anywhere.

    `campaigns` are `Watched` objects (id, name, parent_name). `known_ids` are lead ids already in
    the library and `known_replies` the Gamic replies already in it.
    """
    result = SyncResult()
    names = {c.id: (c.parent_name or c.name) for c in campaigns}
    sequences: dict[int, list[str] | None] = {}
    seen_here: set[str] = set()
    known_replies = list(known_replies)

    def steps_for(campaign_id: int) -> list[str] | None:
        if campaign_id not in sequences:
            try:
                sequences[campaign_id] = step_texts(smartlead.get_sequence(campaign_id))
            except SmartleadError as exc:
                logger.warning("sync: no sequence for campaign %s: %s", campaign_id, exc)
                sequences[campaign_id] = None
        return sequences[campaign_id]

    def handle(item: dict[str, Any]) -> None:
        result.inbox_threads += 1
        try:
            category = int(item.get("lead_category_id") or 0)
        except (TypeError, ValueError):
            return
        if category not in SYNC_CATEGORIES:
            return
        result.in_categories += 1
        lead_id = str(item.get("email_lead_id") or "")
        map_id = str(item.get("email_lead_map_id") or "")
        if not lead_id or {lead_id, map_id} & (known_ids | seen_here):
            result.already_in_library += 1
            return
        campaign_id = int(item["email_campaign_id"])
        steps = steps_for(campaign_id)
        if steps is None:
            result.sequence_unavailable += 1
            return
        try:
            messages = parse_thread(item)
        except ParseError:
            result.errors += 1
            return
        found = sent_after_first_reply(messages, steps)
        if found is None or found[1] is None or not found[1].text.strip():
            result.no_answer += 1
            return
        reply, sent, _ = found
        if any(similarity(sent.text, known) >= ALREADY_HAVE for known in known_replies):
            result.already_in_library += 1
            return
        campaign_name = names.get(campaign_id) or str(item.get("email_campaign_name") or "")
        try:
            outcome = classifier.classify(reply.text, campaign_name, _first_sent_text(messages))
        except Exception as exc:  # noqa: BLE001 - one bad thread must not stop the sync
            logger.error("sync: classifier failed: %s", type(exc).__name__)
            result.errors += 1
            return
        if outcome.confidence < min_confidence:
            result.low_confidence += 1
            return
        if outcome.label in SKIP_LABELS:
            result.skipped_label += 1
            return
        seen_here.update({lead_id, map_id})
        known_replies.append(sent.text)
        result.pairs.append(
            NewExample(
                source=ExampleSource.SMARTLEAD_SYNC,
                source_thread_id=lead_id,
                label=outcome.label,
                prospect_reply=reply.text,
                gamic_reply=sent.text,
                campaign_name=campaign_name,
                replied_at=reply.timestamp,
                sent_at=sent.timestamp,
            )
        )

    ids = [c.id for c in campaigns]
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        offset = 0
        while True:
            if max_threads is not None and result.inbox_threads >= max_threads:
                return result
            try:
                response = smartlead.fetch_master_inbox_replies(
                    batch, limit=PAGE, offset=offset, include_history=True
                )
                items = inbox_items(response)
            except (SmartleadError, ParseError) as exc:
                logger.error("sync: inbox read failed for %s: %s", batch, exc)
                result.errors += 1
                break
            for item in items:
                handle(item)
            if progress:
                progress(f"campaigns {batch}: {offset + len(items)} threads read")
            if len(items) < PAGE:
                break
            offset += PAGE
    return result


def store_pairs(db: Any, pairs: list[NewExample]) -> int:
    """Write the pairs to reply_examples; the number that were new."""
    from app.db.client import insert_example

    return sum(1 for pair in pairs if insert_example(db, pair).is_new)


def known_in_library(db: Any) -> tuple[set[str], list[str]]:
    """(source_thread_ids, Gamic replies) of everything already in reply_examples."""
    from app.db.client import fetch_all_examples

    rows = fetch_all_examples(db)
    return {str(r["source_thread_id"]) for r in rows}, [str(r["gamic_reply"]) for r in rows]


def sync_once(deps: Any, settings: Any) -> SyncResult:
    """One real pass: discover the watched campaigns, read, classify, store. Never raises for one
    bad thread. Used by the daily job in the app."""
    from app.smartlead.watch import watched_campaigns

    campaigns = watched_campaigns(
        deps.smartlead.list_campaigns(), settings.gmc_campaign_ids, settings.ignore_campaign_ids
    )
    ids, replies = known_in_library(deps.db)
    result = sync_examples(
        deps.smartlead,
        deps.classifier,
        campaigns,
        ids,
        replies,
        min_confidence=settings.CLASSIFIER_MIN_CONFIDENCE,
    )
    result.stored = store_pairs(deps.db, result.pairs)
    logger.info(
        "example sync: %d campaigns, %d threads read, %d in the positive categories, "
        "%d new examples stored, %d already in the library, %d without an answer, %d errors",
        len(campaigns),
        result.inbox_threads,
        result.in_categories,
        result.stored,
        result.already_in_library,
        result.no_answer,
        result.errors,
    )
    return result
