"""Post the daily summary of replies that need no action, now.

Usage:
    python scripts/post_digest.py            # DRY RUN: print the summary, post nothing
    python scripts/post_digest.py --apply    # post it to SLACK_ALERT_CHANNEL and mark them listed

The summary lists every waiting reply (unsubscribe, not interested, wrong person, out of office,
automatic reply or bounce), grouped by type, each name linking to the thread in Smartlead. The app
posts the same message by itself every day at DIGEST_HOUR (UTC). With SLACK_ALERT_CHANNEL empty
nothing is posted. Reads the database only; never calls Smartlead, never sends, never suppresses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="post it (default: dry run)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # the summary has emoji; Windows consoles do not

    from slack_sdk import WebClient

    from app.config.settings import get_settings
    from app.db.connect import create_db
    from app.ingest.deps import IngestConfig
    from app.slack.digest import build_digest, collect, post_digest

    settings = get_settings()
    db = create_db(settings)
    config = IngestConfig.from_settings(settings)
    found = collect(db, config)
    print(f"{len(found)} replies are waiting for the summary.")
    if not found:
        return 0
    _, blocks = build_digest([entry for _, entry in found])
    for block in blocks:
        print(block["text"]["text"])
    if not settings.SLACK_ALERT_CHANNEL:
        print("\nSLACK_ALERT_CHANNEL is not set: nothing can be posted (the replies stay stored).")
        return 0
    if not args.apply:
        print("\nDRY RUN: nothing posted. Re-run with --apply to post it.")
        return 0
    slack = WebClient(token=settings.SLACK_BOT_TOKEN)
    posted = post_digest(db, slack, settings.SLACK_ALERT_CHANNEL, config)
    print(f"\nPosted the summary: {posted} replies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
