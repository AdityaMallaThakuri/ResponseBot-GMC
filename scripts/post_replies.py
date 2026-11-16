"""Post a day's replies to Slack as real cards (for the test channel), through the app's pipeline.

Usage:
    python scripts/post_replies.py                       # DRY RUN: the list, nothing posted
    python scripts/post_replies.py --date 2026-11-12
    python scripts/post_replies.py --campaign-id 4317858 --campaign-id 4317852
    python scripts/post_replies.py --apply               # store the threads and post the cards

Default campaigns: the watched campaigns whose status is ACTIVE. Default date: yesterday (UTC).

It does what the webhook would do for each thread whose FIRST prospect reply came on that day:
store the thread and messages, classify, draft, and post the card (draft cards with Good draft /
Bad draft, notices without buttons) to SLACK_DRAFT_CHANNEL (notices to SLACK_ALERT_CHANNEL if
set). Like the live app it skips a thread that already has a card, and a thread whose first
reply was on an earlier day (that day's reply is a follow-up, which the app stores but never
classifies).

For this script only, a card also gets a reply in its Slack thread, "What we actually sent", when
we already answered the lead after their reply with something that is not a sequence step.
Replies that need no action (out of office, unsubscribe, ...) get no card: they wait for the daily
summary (scripts/post_digest.py).

Reads Smartlead (never sends, never changes anything there). Writes to the database and Slack only
with --apply. SEND_ENABLED must be false. Lead email addresses are never printed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))
sys.path.insert(0, str(SCRIPTS))

SHOW = 90  # characters of the reply in the list


def first_reply(messages: list[Any]) -> Any | None:
    from app.smartlead.models import Direction

    replies = sorted(
        (m for m in messages if m.direction is Direction.REPLY), key=lambda m: m.timestamp
    )
    return replies[0] if replies else None


def actually_sent_text(messages: list[Any], steps: list[str]) -> str | None:
    """The reply we sent after the lead's first reply that is not a sequence step, or None."""
    from app.examples.learn import sent_after_first_reply

    found = sent_after_first_reply(messages, steps)
    if found is None or found[1] is None or not found[1].text.strip():
        return None
    return found[1].text.strip()


def plan_item(
    item: dict[str, Any], messages: list[Any], day: date, has_card: bool, sent: str | None
) -> tuple[str, str]:
    """("post" or "skip", why)."""
    first = first_reply(messages)
    if first is None or not any(m.timestamp.date() == day for m in messages if m is first):
        return "skip", "follow-up: its first reply came on an earlier day"
    if has_card:
        return "skip", "already has a Slack card"
    return "post", "has an answer from us already" if sent else "new"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", type=int, action="append")
    parser.add_argument("--date", help="YYYY-MM-DD (UTC); default yesterday")
    parser.add_argument("--apply", action="store_true", help="store and post (default: dry run)")
    args = parser.parse_args()

    import anthropic
    from preview_replies import fetch_items
    from slack_sdk import WebClient

    from app.classify.classifier import Classifier
    from app.config.settings import get_settings
    from app.db.client import fetch_slack_cards, find_thread, insert_message, upsert_thread
    from app.db.connect import create_db
    from app.db.records import NewMessage
    from app.examples.drafter import ExampleDrafter
    from app.examples.learn import step_texts
    from app.examples.library import load_pool
    from app.examples.verticals import VerticalResolver
    from app.ingest.classify_step import handle_first_reply
    from app.ingest.deps import IngestConfig, IngestDeps
    from app.ingest.process import _vertical, build_thread
    from app.smartlead.client import SmartleadClient
    from app.smartlead.models import Lead
    from app.smartlead.thread import parse_thread
    from app.smartlead.watch import watched_campaigns

    settings = get_settings()
    if settings.SEND_ENABLED:
        raise SystemExit("SEND_ENABLED is true: this script refuses to run.")
    day = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date() - timedelta(1)
    smartlead = SmartleadClient.from_settings(settings)  # only ever read here
    db = create_db(settings)
    verticals = VerticalResolver(smartlead.list_campaigns)

    ids = args.campaign_id
    if not ids:
        watched = watched_campaigns(
            smartlead.list_campaigns(), frozenset(), settings.ignore_campaign_ids
        )
        ids = [w.id for w in watched if w.status == "ACTIVE"]
    print(f"{len(ids)} campaigns, replies from {day} (UTC)")

    sequences: dict[int, list[str]] = {}

    def steps_for(campaign_id: int) -> list[str]:
        if campaign_id not in sequences:
            try:
                sequences[campaign_id] = step_texts(smartlead.get_sequence(campaign_id))
            except Exception:  # noqa: BLE001 - without steps, nothing is treated as a step
                sequences[campaign_id] = []
        return sequences[campaign_id]

    candidates: list[tuple[dict[str, Any], list[Any], Any]] = []
    for cid in ids:
        items = fetch_items(smartlead, cid)
        print(f"campaign {cid}: {len(items)} inbox threads scanned")
        for item in items:
            try:
                messages = parse_thread(item)
            except Exception:  # noqa: BLE001 - skip a thread that does not parse
                continue
            todays = [
                m for m in messages if m.direction.value == "REPLY" and m.timestamp.date() == day
            ]
            if todays:
                candidates.append((item, messages, min(todays, key=lambda m: m.timestamp)))
    candidates.sort(key=lambda c: c[2].timestamp)

    rows: list[tuple[str, str, dict[str, Any], list[Any], Any, str | None]] = []
    for item, messages, reply in candidates:
        lead_id, campaign_id = int(item["email_lead_id"]), int(item["email_campaign_id"])
        existing = find_thread(db, lead_id, campaign_id)
        has_card = bool(existing and fetch_slack_cards(db, existing["id"]))
        sent = actually_sent_text(messages, steps_for(campaign_id))
        action, why = plan_item(item, messages, day, has_card, sent)
        rows.append((action, why, item, messages, reply, sent))

    print(f"\n{len(rows)} replies from {day}:\n")
    for n, (action, why, item, _, reply, _) in enumerate(rows, 1):
        short = " ".join(reply.text.split())
        short = short if len(short) <= SHOW else short[: SHOW - 1] + "…"
        name = str(item.get("lead_first_name") or "(no name)")
        campaign = str(item.get("email_campaign_name") or "")
        print(
            f"[{n}] {action.upper():<4} {name} | {campaign} | {reply.timestamp:%H:%M} UTC | {why}"
        )
        print(f"      {short}")
    todo = [r for r in rows if r[0] == "post"]
    print(f"\n{len(todo)} cards to post, {len(rows) - len(todo)} skipped.")
    if not args.apply:
        print("DRY RUN: nothing stored, nothing posted. Re-run with --apply to post the cards.")
        return 0

    claude = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)
    sent_by_thread: dict[tuple[int, int], str | None] = {}
    deps = IngestDeps(
        db=db,
        smartlead=smartlead,
        classifier=Classifier(claude, settings.CLASSIFIER_MODEL),
        slack=WebClient(token=settings.SLACK_BOT_TOKEN),
        example_drafter=ExampleDrafter(
            lambda label, vertical="": load_pool(db, label, vertical),
            claude,
            settings.DRAFT_MODEL,
            settings.PRICE_SENTENCE,
            vertical_of=verticals.for_thread,
            signature=settings.DRAFT_SIGNATURE,
        ),
        verticals=verticals,
        config=IngestConfig.from_settings(settings),
        actually_sent=lambda thread, stored: sent_by_thread.get(
            (int(thread["email_lead_id"]), int(thread["email_campaign_id"]))
        ),
    )
    posted = 0
    for _action, _, item, messages, _, sent in todo:
        lead = Lead(
            campaign_id=int(item["email_campaign_id"]),
            lead_id=int(item["email_lead_id"]),
            email=item.get("lead_email"),
        )
        sent_by_thread[(lead.lead_id, lead.campaign_id)] = sent
        thread = upsert_thread(
            db, build_thread(lead, item, {}, messages, _vertical(deps, lead, item, {}))
        )
        stored = [
            (m, insert_message(db, NewMessage.from_message(thread["id"], m)).row) for m in messages
        ]
        try:
            posted += bool(handle_first_reply(deps, thread, stored, {}))
        except Exception as exc:  # noqa: BLE001 - report and carry on with the next thread
            print(f"  failed for one thread: {type(exc).__name__}: {exc}")
    print(f"\nPosted {posted} cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
