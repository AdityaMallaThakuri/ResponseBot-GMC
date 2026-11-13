"""Remove the test cards of one day so they can be posted again (for card layout comparisons).

Usage:
    python scripts/reset_cards.py                      # DRY RUN: list what would be removed
    python scripts/reset_cards.py --date 2026-11-12    # first replies that day (UTC)
    python scripts/reset_cards.py --apply              # do it

For every thread whose FIRST prospect reply came on that day and that has a card in `slack_cards`:
  - the Slack message is deleted (only the message our bot posted, found through slack_cards),
  - the `slack_cards` row is deleted,
  - the thread's draft is deleted too, but only if it is untouched (still "proposed", no Good/Bad
    feedback, no decision, not saved as an example), so a new post writes a fresh draft.
The thread, messages and classification stay, so posting again does not classify twice.
It never calls Smartlead or the Anthropic API. Lead names are shown by first name only.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def first_reply_day(messages: list[dict[str, Any]]) -> date | None:
    """The UTC date of the thread's first prospect reply, or None if it has none."""
    times = sorted(str(m["sent_at"]) for m in messages if m["direction"] == "REPLY")
    if not times:
        return None
    return datetime.fromisoformat(times[0].replace("Z", "+00:00")).astimezone(UTC).date()


def draft_untouched(db: Any, draft: dict[str, Any]) -> bool:
    if draft.get("status") != "proposed" or draft.get("feedback"):
        return False
    if db.table("decisions").select("id").eq("draft_id", draft["id"]).execute().data:
        return False
    return not db.table("reply_examples").select("id").eq("draft_id", draft["id"]).execute().data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD (UTC); default yesterday")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    from app.config.settings import get_settings
    from app.db.connect import create_db

    settings = get_settings()
    day = date.fromisoformat(args.date) if args.date else datetime.now(UTC).date() - timedelta(1)
    db = create_db(settings)

    found: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = []
    for card in db.table("slack_cards").select("*").execute().data:
        thread = db.table("threads").select("*").eq("id", card["thread_id"]).execute().data
        messages = (
            db.table("messages").select("*").eq("thread_id", card["thread_id"]).execute().data
        )
        if thread and first_reply_day(messages) == day:
            drafts = (
                db.table("drafts").select("*").eq("thread_id", card["thread_id"]).execute().data
            )
            found.append((card, thread[0], drafts))

    print(f"{len(found)} cards for threads whose first reply was on {day}:\n")
    for n, (card, thread, drafts) in enumerate(found, 1):
        name = str(thread.get("lead_first_name") or "(no name)")
        kept = [d for d in drafts if not draft_untouched(db, d)]
        print(
            f"[{n}] {name} | {thread.get('campaign_name')} | lane {card['lane']} | "
            f"status {card['status']} | drafts: {len(drafts)}"
            + (f" ({len(kept)} used, will be kept)" if kept else "")
            + f" | slack message {'yes' if card.get('slack_ts') else 'none'}"
        )
    if not args.apply:
        print("\nDRY RUN: nothing deleted. Re-run with --apply.")
        return 0

    slack = WebClient(token=settings.SLACK_BOT_TOKEN)
    for card, _, drafts in found:
        if card.get("slack_ts") and card.get("channel"):
            try:
                slack.chat_delete(channel=card["channel"], ts=card["slack_ts"])
            except SlackApiError as exc:
                print(f"  slack: {exc.response.get('error')} (the row is removed anyway)")
        db.table("slack_cards").delete().eq("id", card["id"]).execute()
        for draft in drafts:
            if draft_untouched(db, draft):
                db.table("drafts").delete().eq("id", draft["id"]).execute()
    print(f"\nRemoved {len(found)} cards (Slack message, card row, untouched draft).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
