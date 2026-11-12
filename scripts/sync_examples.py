"""Sync reply examples from Smartlead (read only): what Ray really sent to positive leads.

Usage:
    python scripts/sync_examples.py                    # DRY RUN: store nothing
    python scripts/sync_examples.py --max-threads 200  # dry run on the first 200 inbox threads
    python scripts/sync_examples.py --out eval/data/synced_examples.json   # also save the pairs
    python scripts/sync_examples.py --apply            # store new pairs in reply_examples

Needs migration 0013 applied before --apply. Reads Smartlead (never writes to it) and calls the
Anthropic API to classify each prospect reply. A dry run reads the library only to skip threads it
already has (it carries on with an empty library if the table is not there yet).
See app/examples/sync.py for the rules and the categories used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="store the new pairs (default: dry run)"
    )
    parser.add_argument("--max-threads", type=int, default=None, help="stop after N inbox threads")
    parser.add_argument("--out", type=Path, default=None, help="save the pairs to this JSON file")
    args = parser.parse_args()

    import anthropic

    from app.classify.classifier import Classifier
    from app.config.settings import get_settings
    from app.examples.sync import (
        SKIP_LABELS,
        SYNC_CATEGORIES,
        known_in_library,
        store_pairs,
        sync_examples,
    )
    from app.smartlead.client import SmartleadClient
    from app.smartlead.watch import watched_campaigns

    settings = get_settings()
    smartlead = SmartleadClient.from_settings(settings)
    classifier = Classifier(
        anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5),
        settings.CLASSIFIER_MODEL,
    )
    campaigns = watched_campaigns(
        smartlead.list_campaigns(), settings.gmc_campaign_ids, settings.ignore_campaign_ids
    )
    print(f"{len(campaigns)} watched campaigns (GMC and POS REPLY children)")
    print("Categories used: " + ", ".join(f"{n} ({i})" for i, n in SYNC_CATEGORIES.items()))

    db = None
    known_ids: set[str] = set()
    known_replies: list[str] = []
    try:
        from app.db.connect import create_db

        db = create_db(settings)
        known_ids, known_replies = known_in_library(db)
        print(f"Library already holds {len(known_ids)} examples")
    except Exception as exc:  # noqa: BLE001 - a dry run works without the table
        if args.apply:
            raise SystemExit(
                f"cannot read the library ({type(exc).__name__}); not storing"
            ) from None
        print(f"Library not readable ({type(exc).__name__}): continuing with an empty one")

    result = sync_examples(
        smartlead,
        classifier,
        campaigns,
        known_ids,
        known_replies,
        min_confidence=settings.CLASSIFIER_MIN_CONFIDENCE,
        max_threads=args.max_threads,
        progress=lambda line: print("  " + line, flush=True),
    )

    print(
        f"\n{result.inbox_threads} inbox threads read, {result.in_categories} in the positive "
        f"categories, {len(result.pairs)} new pairs"
    )
    print(
        f"Skipped: {result.already_in_library} already in the library, {result.no_answer} with no "
        f"answer from us that is not a sequence step, {result.skipped_label} with a label that is "
        f"no example ({', '.join(sorted(SKIP_LABELS))}), "
        f"{result.low_confidence} low confidence, {result.sequence_unavailable} with no readable "
        f"sequence, {result.errors} errors"
    )
    print("\nPairs per label:")
    for label, n in result.by_label.most_common():
        print(f"  {n:4d}  {label}")
    print("\nPairs per campaign:")
    for name, n in result.by_campaign.most_common():
        print(f"  {n:4d}  {name}")
    span = result.date_range()
    if span:
        print(f"\nOur replies were sent between {span[0]:%Y-%m-%d} and {span[1]:%Y-%m-%d}")

    if args.out:
        rows = [
            {
                "source_thread_id": p.source_thread_id,
                "label": p.label,
                "campaign_name": p.campaign_name,
                "prospect_reply": p.prospect_reply,
                "gamic_reply": p.gamic_reply,
                "replied_at": p.replied_at.isoformat() if p.replied_at else None,
                "sent_at": p.sent_at.isoformat() if p.sent_at else None,
            }
            for p in result.pairs
        ]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nSaved {len(rows)} pairs to {args.out} (keep it out of git: it holds reply text)")

    if not args.apply:
        print("\nDRY RUN: nothing stored. Run with --apply to store these pairs.")
        return
    stored = store_pairs(db, result.pairs)
    print(f"\nStored {stored} new examples ({len(result.pairs) - stored} were already there)")


if __name__ == "__main__":
    main()
