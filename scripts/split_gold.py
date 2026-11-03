"""Split gold_set.csv into a tuning set and a held-out set (B5).

Usage: python scripts/split_gold.py [--holdout 50] [--seed 7] [--force]

Picks the held-out thread_ids with a fixed seed, stratified by human_message_type, and saves
them to eval/data/holdout_ids.txt. Everything else is the tuning set. Refuses to overwrite an
existing split, because re-splitting would leak held-out rows into tuning. Prints counts only.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval" / "data" / "gold_set.csv"
HOLDOUT = ROOT / "eval" / "data" / "holdout_ids.txt"


def allocate(counts: dict[str, int], target: int, rng: random.Random) -> dict[str, int]:
    """Held-out rows per label: proportional, largest remainders first, never above the count."""
    total = sum(counts.values())
    quota = {k: v * target / total for k, v in counts.items()}
    alloc = {k: int(q) for k, q in quota.items()}
    order = sorted(counts, key=lambda k: (-(quota[k] - alloc[k]), rng.random()))
    for label in order:
        if sum(alloc.values()) >= target:
            break
        if alloc[label] < counts[label]:
            alloc[label] += 1
    return alloc


def split(rows: list[dict[str, str]], holdout: int, seed: int) -> list[str]:
    """The held-out thread_ids, stratified by label and chosen with a fixed seed."""
    rng = random.Random(seed)
    by_label: dict[str, list[str]] = {}
    for r in sorted(rows, key=lambda r: r["thread_id"]):
        by_label.setdefault(r["human_message_type"], []).append(r["thread_id"])
    alloc = allocate({k: len(v) for k, v in by_label.items()}, holdout, rng)
    chosen: list[str] = []
    for label in sorted(by_label):
        chosen += rng.sample(by_label[label], alloc[label])
    return sorted(chosen)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold", type=Path, default=GOLD)
    p.add_argument("--out", type=Path, default=HOLDOUT)
    p.add_argument("--holdout", type=int, default=50)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--force", action="store_true", help="overwrite an existing split")
    args = p.parse_args(argv)

    if args.out.exists() and not args.force:
        print(f"{args.out} already exists. Re-splitting leaks held-out rows; use --force.")
        return 1
    with args.gold.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    ids = split(rows, args.holdout, args.seed)
    args.out.write_text("\n".join(ids) + "\n", encoding="utf-8")

    held = set(ids)
    by_held = Counter(r["human_message_type"] for r in rows if r["thread_id"] in held)
    by_tune = Counter(r["human_message_type"] for r in rows if r["thread_id"] not in held)
    print(f"Held out {len(held)} of {len(rows)} rows, saved to {args.out}")
    print("Held-out per label:", dict(sorted(by_held.items())))
    print("Tuning per label:  ", dict(sorted(by_tune.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
