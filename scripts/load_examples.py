"""Fill the reply_examples table from the past replies dataset (source = "history").

Usage:
    python scripts/load_examples.py --dry-run     # count only, no database
    python scripts/load_examples.py               # insert (needs migration 0011 applied)

Safe to re-run: a thread already stored is left alone. Writes only to reply_examples. It never
calls Smartlead, Slack or the Anthropic API.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from app.config.settings import get_settings
    from app.examples.history import load_history
    from app.examples.verticals import VerticalResolver
    from app.smartlead.client import SmartleadClient

    # Read only: the campaign list gives each POS REPLY row its parent's vertical.
    verticals = VerticalResolver(SmartleadClient.from_settings(get_settings()).list_campaigns)
    examples, skipped = load_history(verticals=verticals)
    print(
        f"{len(examples)} examples ready, "
        f"{skipped} dataset rows skipped (no reply, response or label)"
    )
    for label, n in Counter(e.label for e in examples).most_common():
        print(f"  {n:4d}  {label}")
    for vertical, n in Counter(e.vertical for e in examples).most_common():
        print(f"  vertical {vertical}: {n}")
    if args.dry_run:
        return

    from app.db.client import insert_example
    from app.db.connect import create_db

    db = create_db(get_settings())
    new = 0
    for example in examples:
        new += insert_example(db, example).is_new
    print(f"inserted {new} new, {len(examples) - new} already stored")


if __name__ == "__main__":
    main()
