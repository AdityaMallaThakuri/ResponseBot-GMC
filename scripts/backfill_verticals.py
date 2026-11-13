"""Set the vertical on stored examples and threads (needs migration 0014 for --apply).

Usage:
    python scripts/backfill_verticals.py                    # DRY RUN: counts only, writes nothing
    python scripts/backfill_verticals.py --show-campaigns   # also list every campaign by group
    python scripts/backfill_verticals.py --apply            # write the verticals

How a row is placed (rules in app/config/verticals.yaml):
  - history examples: through the dataset's campaign_id, so a POS REPLY row takes its parent's;
  - other examples and threads: through the thread's email_campaign_id (the campaign tree);
  - SOP templates: ma;
  - anything left: the campaign name.
Reads Smartlead (campaign list, read only) and Supabase. Writes only reply_examples.vertical and
threads.vertical, and only with --apply. It never touches Slack or sends anything.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def campaign_groups(
    campaigns: list[dict[str, Any]], watched: list[Any], resolver: Any
) -> dict[str, dict[str, list[str]]]:
    """vertical -> {"campaigns": [parent names], "children": [child names]} for watched ones."""
    groups: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"campaigns": [], "children": []})
    for w in watched:
        vertical = resolver.for_campaign(w.id, w.name)
        groups[vertical]["children" if w.parent_name else "campaigns"].append(w.name)
    return groups


def dataset_campaigns(data_dir: Path) -> dict[str, int]:
    """thread_id -> campaign_id from the past replies dataset (empty if the file is missing)."""
    from app.examples.history import read_csv

    path = data_dir / "gmc_all_replies_dataset.csv"
    if not path.exists():
        return {}
    return {
        r["thread_id"]: int(r["campaign_id"])
        for r in read_csv(path)
        if (r.get("campaign_id") or "").strip().isdigit()
    }


def example_vertical(
    row: dict[str, Any],
    resolver: Any,
    dataset: dict[str, int],
    thread_campaigns: dict[str, tuple[int, str]],
) -> str:
    """The vertical one stored example should have."""
    if row["source"] == "sop":
        return "ma"
    name = str(row.get("campaign_name") or "")
    if row["source"] == "history" and str(row["source_thread_id"]) in dataset:
        return resolver.for_campaign(dataset[str(row["source_thread_id"])], name)
    thread = thread_campaigns.get(str(row.get("thread_id") or ""))
    if thread:
        return resolver.for_campaign(thread[0], thread[1] or name)
    return resolver.for_campaign(None, name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--show-campaigns", action="store_true")
    args = parser.parse_args()

    from app.config.settings import get_settings
    from app.db.client import update_example_vertical, update_thread_vertical
    from app.db.connect import create_db
    from app.examples.history import DATA
    from app.examples.verticals import VERTICALS, VerticalResolver
    from app.smartlead.client import SmartleadClient
    from app.smartlead.watch import watched_campaigns

    settings = get_settings()
    campaigns = SmartleadClient.from_settings(settings).list_campaigns()
    resolver = VerticalResolver(lambda: campaigns)
    # Automatic discovery (the GMC_CAMPAIGN_IDS testing override is ignored on purpose).
    watched = watched_campaigns(campaigns, frozenset(), settings.ignore_campaign_ids)

    groups = campaign_groups(campaigns, watched, resolver)
    print(f"{len(watched)} watched campaigns ({len(campaigns)} on the account)\n")
    print(f"{'vertical':<12}{'campaigns':>10}{'children':>10}{'total':>8}")
    for vertical in VERTICALS:
        g = groups.get(vertical)
        if g:
            n, c = len(g["campaigns"]), len(g["children"])
            print(f"{vertical:<12}{n:>10}{c:>10}{n + c:>8}")
    if args.show_campaigns:
        for vertical in VERTICALS:
            g = groups.get(vertical)
            if g:
                print(f"\n[{vertical}] ({len(g['campaigns'])} campaigns)")
                for name in sorted(g["campaigns"]):
                    print(f"  {name}")
    else:
        unknown = groups.get("unknown")
        if unknown:
            print("\nunknown (counts as NOT M&A): " + "; ".join(sorted(unknown["campaigns"])))

    db = create_db(settings)
    examples = db.table("reply_examples").select("*").execute().data
    threads = db.table("threads").select("*").execute().data
    has_column = not examples or "vertical" in examples[0]
    if not has_column:
        print("\nreply_examples has no vertical column yet: apply migration 0014 first.")
    thread_campaigns = {
        str(t["id"]): (int(t["email_campaign_id"]), str(t.get("campaign_name") or ""))
        for t in threads
    }
    dataset = dataset_campaigns(DATA)
    print(f"\n{len(examples)} stored examples, {len(threads)} threads, {len(dataset)} dataset rows")

    proposed = [(e, example_vertical(e, resolver, dataset, thread_campaigns)) for e in examples]
    by_vertical = Counter(v for _, v in proposed)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    for e, v in proposed:
        by_source[e["source"]][v] += 1
    print("\nExamples per vertical (what the backfill would set):")
    for vertical in VERTICALS:
        if by_vertical[vertical]:
            detail = ", ".join(
                f"{s} {c[vertical]}" for s, c in sorted(by_source.items()) if c[vertical]
            )
            print(f"  {vertical:<12}{by_vertical[vertical]:>5}   ({detail})")
    ma_n = by_vertical["ma"]
    print(f"  M&A side {ma_n}, everything else {len(proposed) - ma_n}")

    ex_changes = [(e, v) for e, v in proposed if e.get("vertical", "unknown") != v]
    th_changes = [
        (t, resolver.for_campaign(int(t["email_campaign_id"]), str(t.get("campaign_name") or "")))
        for t in threads
        if not t.get("vertical")
    ]
    print(f"\nWould change {len(ex_changes)} examples and {len(th_changes)} threads.")
    if not args.apply:
        print("DRY RUN: nothing written. Run with --apply (after migration 0014) to write.")
        return
    if not has_column:
        raise SystemExit("apply migration 0014 first")
    for e, v in ex_changes:
        update_example_vertical(db, e["id"], v)
    for t, v in th_changes:
        update_thread_vertical(db, t["id"], v)
    print(f"Wrote {len(ex_changes)} examples and {len(th_changes)} threads.")


if __name__ == "__main__":
    main()
