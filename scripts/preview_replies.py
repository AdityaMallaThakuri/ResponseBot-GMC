"""Show the replies one campaign got on a day, and the draft the bot would write for each.

Usage:
    python scripts/preview_replies.py --campaign-id 4317858 --campaign-id 4317852
    python scripts/preview_replies.py --campaign-id 4317858 --date 2026-11-11 --max 10

Default date: yesterday (UTC). Read only: it reads Smartlead's master inbox (never sends,
categorises or changes anything), reads the example library from the database, and calls the
Anthropic API to classify and draft. It posts nothing to Slack and stores nothing. The full text
goes to eval/reports/preview_replies.md (gitignored) and a shorter view is printed. Lead email
addresses are never printed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPORT = Path(__file__).resolve().parent.parent / "eval" / "reports" / "preview_replies.md"
MAX_PAGES = 30
SHOW = 600  # characters of a reply shown in the terminal


def fetch_items(client: Any, campaign_id: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        response = client.fetch_master_inbox_replies(
            [campaign_id], limit=20, offset=page * 20, include_history=True
        )
        batch = response.get("data") or []
        items.extend(batch)
        if len(batch) < 20:
            break
    return items


def replies_on(messages: list[Any], day: date) -> list[Any]:
    from app.smartlead.models import Direction

    return [m for m in messages if m.direction is Direction.REPLY and m.timestamp.date() == day]


def outreach_before(messages: list[Any], reply: Any) -> str:
    from app.smartlead.models import Direction

    for m in messages:
        if m.direction is Direction.SENT and m.timestamp <= reply.timestamp:
            return m.text
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", type=int, action="append", required=True)
    parser.add_argument("--date", help="YYYY-MM-DD (UTC); default yesterday")
    parser.add_argument("--max", type=int, default=15, help="most replies to classify and draft")
    args = parser.parse_args()

    import anthropic

    from app.classify.classifier import Classifier
    from app.classify.routing import LANE_DRAFT, route
    from app.config.settings import get_settings
    from app.db.connect import create_db
    from app.examples.drafter import ExampleDrafter
    from app.examples.library import load_pool
    from app.examples.verticals import VerticalResolver
    from app.smartlead.client import SmartleadClient
    from app.smartlead.thread import parse_thread

    settings = get_settings()
    day = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date() - timedelta(1)
    client = SmartleadClient(settings.SMARTLEAD_API_KEY)  # read only here
    db = create_db(settings)
    claude = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)
    classifier = Classifier(claude, settings.CLASSIFIER_MODEL)
    verticals = VerticalResolver(client.list_campaigns)
    drafter = ExampleDrafter(
        lambda label, vertical="": load_pool(db, label, vertical),
        claude,
        settings.DRAFT_MODEL,
        settings.PRICE_SENTENCE,
        vertical_of=verticals.for_thread,
    )

    found: list[tuple[dict[str, Any], Any, list[Any]]] = []
    for cid in args.campaign_id:
        items = fetch_items(client, cid)
        print(f"campaign {cid}: {len(items)} inbox threads scanned")
        for item in items:
            try:
                messages = parse_thread(item)
            except Exception:  # noqa: BLE001 - skip a thread that does not parse
                print(f"  (skipped one unparsable thread in {cid})")
                continue
            for reply in replies_on(messages, day):
                found.append((item, reply, messages))
    found.sort(key=lambda f: f[1].timestamp)
    print(f"\n{len(found)} replies on {day} (UTC). Showing {min(len(found), args.max)}.\n")

    lines = [f"# Replies on {day} and the drafts the bot would write\n"]
    for n, (item, reply, messages) in enumerate(found[: args.max], 1):
        campaign = str(item.get("email_campaign_name") or "")
        first = str(item.get("lead_first_name") or "")
        outreach = outreach_before(messages, reply)
        result = classifier.classify(reply.text, campaign, outreach)
        plan = route(
            result,
            settings.CLASSIFIER_MIN_CONFIDENCE,
            settings.CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED,
        )
        head = (
            f"[{n}] {first or '(no name)'}  |  {campaign}  |  "
            f"{reply.timestamp:%H:%M} UTC  |  {result.label} {result.confidence:.2f}"
            f"  |  lane: {plan.lane}"
        )
        body = f"Draft:\n{'-' * 8}\n"
        if plan.lane == LANE_DRAFT:
            outcome = drafter.draft(
                thread={
                    "campaign_name": campaign,
                    "lead_first_name": first,
                    "email_campaign_id": item.get("email_campaign_id"),
                },
                label=result.label,
                confidence=result.confidence,
                reply=reply.text,
                outreach=outreach,
                min_confidence=settings.DRAFT_MIN_CONFIDENCE,
            )
            if outcome.kind == "drafted":
                body += outcome.text or ""
                if outcome.flagged:
                    body += f"\n\n!! check these figures: {', '.join(outcome.flagged)}"
                if outcome.removed:
                    body += f"\n(removed {outcome.removed} sentence(s) with unconfirmed numbers)"
                if outcome.price_confirmed is False:
                    body += "\n(price not confirmed: check with Aryan)"
            else:
                body += f"(no draft: {outcome.reason})"
        else:
            note = plan.note or plan.reason
            body += f"(no draft: {note})"
        reply_short = " ".join(reply.text.split())
        shown = reply_short if len(reply_short) <= SHOW else reply_short[: SHOW - 1] + "…"
        print(head)
        print(f"Reply: {shown}\n{body}\n{'=' * 70}")
        lines += [f"## {head}\n", f"**Reply**\n\n{reply.text}\n", f"**{body}**\n"]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Full text: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
