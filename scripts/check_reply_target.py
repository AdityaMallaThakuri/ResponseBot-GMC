"""Read-only: show which stats_id a reply to a thread would use (C4).

Usage:
    python scripts/check_reply_target.py --thread-id <uuid>              # a thread in our database
    python scripts/check_reply_target.py --campaign-id 123 --lead-id 456  # straight from Smartlead

reply_to_stats_id is assumed to be the stats_id of the lead's latest REPLY. That is unverified until
a real test send. This script fetches the thread from Smartlead (a read: message-history), lists the
messages with their ids and shows the one we would reply to, so you can confirm it in Smartlead's
inbox first. It calls no write route and prints no email text; the lead's email is masked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.smartlead.models import Direction, Lead, Thread  # noqa: E402


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    head, _, tld = domain.rpartition(".")
    return f"{local[:1]}***@{head[:1]}***.{tld}" if domain else "***"


def describe(thread: Thread, stored_stats_id: str | None = None) -> list[str]:
    """Lines for a fetched thread: every message (ids only) and the stats_id we would reply to."""
    lines = [
        f"campaign {thread.lead.campaign_id}, lead {thread.lead.lead_id}, "
        f"recipient {mask_email(thread.to_email or thread.lead.email or '')}",
        f"{len(thread.messages)} messages. Newest last; the reply goes to the newest REPLY:",
    ]
    target = thread.reply_to_stats_id
    for i, m in enumerate(thread.messages, 1):
        mark = (
            "  <== we would reply to this"
            if m.direction is Direction.REPLY and m.stats_id == target
            else ""
        )
        lines.append(
            f"  {i}. {m.direction.value:5} {m.timestamp.isoformat()} stats_id={m.stats_id}"
            f" message_id={m.message_id[:30]}{mark}"
        )
    if target is None:
        lines.append("There is no REPLY in this thread: a reply could not be sent.")
        return lines
    shared = [m for m in thread.messages if m.stats_id == target and m.direction is Direction.SENT]
    lines.append(f"reply_to_stats_id = {target}")
    if shared:
        lines.append("  note: a SENT message shares this stats_id (expected: they can share one).")
    if stored_stats_id is not None:
        same = stored_stats_id == target
        lines.append(
            "  our stored latest REPLY has the "
            + ("same stats_id" if same else f"DIFFERENT stats_id {stored_stats_id}")
        )
    lines.append(
        "Confirm in the Smartlead inbox that this is the message the lead sent, then send."
    )
    return lines


def stored_latest_reply_stats_id(db: Any, thread_row: dict[str, Any]) -> str | None:
    from app.db.client import fetch_messages

    replies = [m for m in fetch_messages(db, thread_row["id"]) if m["direction"] == "REPLY"]
    replies.sort(key=lambda m: m["sent_at"])
    return replies[-1]["stats_id"] if replies else None


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--thread-id", help="a thread id from our database")
    p.add_argument("--campaign-id", type=int)
    p.add_argument("--lead-id", type=int)
    args = p.parse_args(argv)

    from app.config.settings import get_settings
    from app.smartlead.client import SmartleadClient

    settings = get_settings()
    stored: str | None = None
    if args.thread_id:
        from app.db.client import get_thread
        from app.db.connect import create_db

        db = create_db(settings)
        row = get_thread(db, args.thread_id)
        if row is None:
            print("thread not found")
            return 1
        lead = Lead(int(row["email_campaign_id"]), int(row["email_lead_id"]), row["lead_email"])
        stored = stored_latest_reply_stats_id(db, row)
    elif args.campaign_id and args.lead_id:
        lead = Lead(args.campaign_id, args.lead_id)
    else:
        p.error("give --thread-id, or both --campaign-id and --lead-id")
    client = SmartleadClient.from_settings(settings)  # only get_thread (a read) is called
    thread = client.get_thread(lead)
    if thread.to_email is None and lead.email is None and thread.messages:
        replies = [m for m in thread.messages if m.direction is Direction.REPLY]
        thread = Thread(
            thread.lead,
            thread.messages,
            thread.reply_to_stats_id,
            replies[-1].from_addr if replies else None,
        )
    print("\n".join(describe(thread, stored)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
