"""Remove ONE old test Slack card row (slack_cards) so a stored thread can be posted again.

Usage:
    python scripts/remove_test_card.py --card-id <uuid>            # dry run: shows the row
    python scripts/remove_test_card.py --card-id <uuid> --apply    # deletes that one row

Deletes only the slack_cards row with that id, and only if it is a draft-less human_only card.
It touches nothing else (the thread, messages, classification and the Slack message stay). It never
calls Smartlead, Slack or the Anthropic API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    from app.config.settings import get_settings
    from app.db.connect import create_db

    db = create_db(get_settings())
    rows = db.table("slack_cards").select("*").eq("id", args.card_id).execute().data
    if len(rows) != 1:
        print("No such card. Nothing changed.")
        return 1
    card = rows[0]
    keys = ("id", "thread_id", "lane", "status", "draft_id", "created_at")
    summary = {k: card.get(k) for k in keys}
    print("card:", summary)
    if card.get("draft_id") is not None or card.get("lane") != "human_only":
        print("Refusing: only a draft-less human_only card may be removed.")
        return 1
    if not args.apply:
        print("Dry run: this one row would be deleted. Re-run with --apply.")
        return 0
    db.table("slack_cards").delete().eq("id", args.card_id).execute()
    print("Deleted 1 card.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
