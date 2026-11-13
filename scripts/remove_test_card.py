"""Remove ONE old test Slack card row (slack_cards) so a stored thread can be posted again.

Usage:
    python scripts/remove_test_card.py --card-id <uuid>            # dry run: shows the row
    python scripts/remove_test_card.py --card-id <uuid> --apply    # deletes that one row
    add --with-draft to also delete the card's own draft (only if it is still untouched)

Deletes only the slack_cards row with that id, and only if it is a draft-less human_only card.
With --with-draft a draft-lane card may go too, together with its draft, but only if that draft is
still "proposed", has no feedback, no decision and no saved example. It touches nothing else
(the thread, messages, classification and the Slack message stay). It never
calls Smartlead, Slack or the Anthropic API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def draft_problem(db, draft_id: str) -> str | None:
    """Why this draft must not be deleted, or None if it is untouched."""
    drafts = db.table("drafts").select("*").eq("id", draft_id).execute().data
    if len(drafts) != 1:
        return "draft not found"
    d = drafts[0]
    if d.get("status") != "proposed" or d.get("feedback"):
        return "it has been used: status or feedback set"
    if db.table("decisions").select("id").eq("draft_id", draft_id).execute().data:
        return "it has a decision"
    if db.table("reply_examples").select("id").eq("draft_id", draft_id).execute().data:
        return "it was saved as an example"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--with-draft", action="store_true")
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
    draft_id = card.get("draft_id")
    if draft_id is None:
        if card.get("lane") != "human_only":
            print("Refusing: only a draft-less human_only card may be removed.")
            return 1
    else:
        problem = draft_problem(db, draft_id) if args.with_draft else "use --with-draft"
        if problem:
            print(f"Refusing: this card has a draft ({problem}).")
            return 1
        print(f"also: draft {draft_id} (untouched, proposed)")
    if not args.apply:
        print("Dry run: only the row(s) above would be deleted. Re-run with --apply.")
        return 0
    db.table("slack_cards").delete().eq("id", args.card_id).execute()
    if draft_id is not None:
        db.table("drafts").delete().eq("id", draft_id).execute()
    print("Deleted 1 card" + (" and its draft." if draft_id is not None else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
