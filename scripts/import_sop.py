"""Import Ray's SOP reply templates as examples (source "sop", highest priority for the label).

Usage:
    python scripts/import_sop.py path/to/sop.md            # DRY RUN: show what would be stored
    python scripts/import_sop.py path/to/sop.md --apply    # store (needs migration 0013)

One template per section, the situation as the heading. Put the label in the heading to be sure:
`## They ask what it costs [price_question]`; otherwise it is matched on keywords, and a heading
that matches nothing is listed and skipped. A template already stored (same label and situation)
is left as it is. Reads a local file and writes only to reply_examples.
See app/examples/sop.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--vertical",
        default="ma",
        help="vertical of every template in the file (default ma: the GMC M&A SOP)",
    )
    args = parser.parse_args()

    from app.examples.sop import sop_examples
    from app.examples.verticals import VERTICALS

    if args.vertical not in VERTICALS:
        raise SystemExit(f"unknown vertical {args.vertical!r}; use one of {', '.join(VERTICALS)}")
    examples, unmatched = sop_examples(
        args.file.read_text(encoding="utf-8-sig"), vertical=args.vertical
    )
    print(f"{len(examples)} templates ready (vertical: {args.vertical})")
    for e in examples:
        print(f"  [{e.label}] {e.prospect_reply[:70]}  ({len(e.gamic_reply.split())} words)")
    for heading in unmatched:
        print(f"  NO LABEL, skipped: {heading[:80]}  (add [label] to the heading)")
    if not args.apply:
        print("\nDRY RUN: nothing stored. Run with --apply to store them.")
        return

    from app.config.settings import get_settings
    from app.db.client import insert_example
    from app.db.connect import create_db

    db = create_db(get_settings())
    new = sum(insert_example(db, e).is_new for e in examples)
    print(f"\nStored {new} new templates, {len(examples) - new} already there")


if __name__ == "__main__":
    main()
