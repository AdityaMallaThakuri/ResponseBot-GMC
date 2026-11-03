"""Build the blind human-review sheet and its answer key (B3).

Usage: python scripts/make_review_sheet.py [--seed 7] [--total 200] [--force]

Reads eval/data/gmc_all_replies_dataset.csv and eval/data/draft_labels.csv (never modified).
Writes, both gitignored:
  eval/data/review_sheet.csv  blind sheet for the labeler: NO draft labels, rows shuffled
  eval/data/review_key.csv    thread_id, sample_reason and the draft labels

Only counts are printed, never message text.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "data" / "gmc_all_replies_dataset.csv"
DRAFTS = ROOT / "eval" / "data" / "draft_labels.csv"
SHEET = ROOT / "eval" / "data" / "review_sheet.csv"
KEY = ROOT / "eval" / "data" / "review_key.csv"

RARE_LABELS = ("trial_request", "objection_info", "objection_price", "booked_confirmation")
LABEL_FLOOR = 6  # minimum threads per label in the sample, where available
LOW_CONFIDENCE = 20
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

SHEET_FIELDS = [
    "thread_id", "campaign_name", "smartlead_category", "outreach_snippet",
    "prospect_first_reply", "gamic_response_to_first_reply",
    "human_message_type", "human_verdict", "human_template", "unsure", "notes",
]  # fmt: skip
KEY_FIELDS = [
    "thread_id", "sample_reason", "message_type", "lead_verdict", "playbook_template", "confidence",
]  # fmt: skip


def mask(text: str) -> str:
    return EMAIL_RE.sub("[email]", text)


def trim(text: str, limit: int) -> str:
    """Mask emails first (so a mask is never cut in half), then trim to limit characters."""
    return mask(text.strip())[:limit]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class Sampler:
    """Draws threads that are not yet picked, remembering why each was picked."""

    def __init__(self, drafts: list[dict[str, Any]], seed: int) -> None:
        self.drafts = sorted(drafts, key=lambda d: d["thread_id"])  # stable base order
        self.rng = random.Random(seed)
        self.picked: dict[str, str] = {}

    def pool(self, **match: str) -> list[dict[str, Any]]:
        return [
            d
            for d in self.drafts
            if d["thread_id"] not in self.picked and all(d[k] == v for k, v in match.items())
        ]

    def take(self, candidates: list[dict[str, Any]], n: int, reason: str) -> int:
        """Pick up to n of candidates at random. Returns how many were picked."""
        chosen = self.rng.sample(candidates, min(n, len(candidates))) if n > 0 else []
        for d in chosen:
            self.picked[d["thread_id"]] = reason
        return len(chosen)

    def count(self, label: str) -> int:
        by_id = {d["thread_id"]: d for d in self.drafts}
        return sum(1 for t in self.picked if by_id[t]["message_type"] == label)


def sample_threads(drafts: list[dict[str, Any]], seed: int, total: int = 200) -> dict[str, str]:
    """Return {thread_id: sample_reason}, in the order the steps picked them."""
    s = Sampler(drafts, seed)

    # 1. every thread of the rare labels
    for label in RARE_LABELS:
        for d in s.pool(message_type=label):
            s.picked[d["thread_id"]] = f"rare_label:{label}"

    # 2. do_not_contact: at least 10 from other Smartlead categories
    dnc = s.pool(message_type="do_not_contact")
    other = [d for d in dnc if d["category_name"] != "Do Not Contact"]
    got = s.take(other, 10, "do_not_contact:other_category")
    s.take(s.pool(message_type="do_not_contact"), 25 - got, "do_not_contact:random")

    # 3. wrong_person: some from Out Of Office and Do Not Contact categories
    got = 0
    for category in ("Out Of Office", "Do Not Contact"):
        pool = s.pool(message_type="wrong_person", category_name=category)
        got += s.take(pool, 5, f"wrong_person:category={category}")
    s.take(s.pool(message_type="wrong_person"), 15 - got, "wrong_person:random")

    # 4. off_script
    s.take(s.pool(message_type="off_script"), 15, "off_script")

    # 5. lowest confidence not already picked
    lowest = sorted(s.pool(), key=lambda d: (d["confidence"], d["thread_id"]))[:LOW_CONFIDENCE]
    for d in lowest:
        s.picked[d["thread_id"]] = "low_confidence"

    # 6. label floor, then random fill
    for label in sorted({d["message_type"] for d in s.drafts}):
        s.take(s.pool(message_type=label), LABEL_FLOOR - s.count(label), f"label_floor:{label}")
    if len(s.picked) > total:
        raise SystemExit(f"steps 1 to 6 already picked {len(s.picked)} threads, over {total}")
    s.take(s.pool(), total - len(s.picked), "random")
    if len(s.picked) != total:
        raise SystemExit(f"only {len(s.picked)} threads available, need {total}")
    return s.picked


def usable_drafts(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []
    for r in rows:
        if r.get("status") == "ok":
            drafts.append({**r, "confidence": float(r["confidence"])})
    return drafts


def sheet_row(data: dict[str, str]) -> dict[str, str]:
    row = {k: "" for k in SHEET_FIELDS}
    row.update(
        thread_id=data["thread_id"],
        campaign_name=mask(data["campaign_name"].strip()),
        smartlead_category=data["category_name"],
        outreach_snippet=trim(data["outreach_email"], 300),
        prospect_first_reply=trim(data["prospect_first_reply"], 800),
        gamic_response_to_first_reply=trim(data["gamic_response_to_first_reply"], 400),
    )
    return row


def has_human_labels(path: Path) -> bool:
    return path.exists() and any(r.get("human_message_type", "").strip() for r in read_csv(path))


def build(
    dataset_rows: list[dict[str, str]], draft_rows: list[dict[str, str]], seed: int, total: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    drafts = usable_drafts(draft_rows)
    reasons = sample_threads(drafts, seed, total)
    by_data = {r["thread_id"]: r for r in dataset_rows}
    by_draft = {d["thread_id"]: d for d in drafts}
    key = [
        {
            "thread_id": t,
            "sample_reason": reason,
            "message_type": by_draft[t]["message_type"],
            "lead_verdict": by_draft[t]["lead_verdict"],
            "playbook_template": by_draft[t]["playbook_template"],
            "confidence": str(by_draft[t]["confidence"]),
        }
        for t, reason in reasons.items()
    ]
    sheet = [sheet_row(by_data[t]) for t in reasons]
    random.Random(seed + 1).shuffle(sheet)  # order must not reveal the sampling steps
    return sheet, key


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, default=DATASET)
    p.add_argument("--drafts", type=Path, default=DRAFTS)
    p.add_argument("--sheet", type=Path, default=SHEET)
    p.add_argument("--key", type=Path, default=KEY)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--total", type=int, default=200)
    p.add_argument("--force", action="store_true", help="overwrite a sheet that has human labels")
    args = p.parse_args(argv)

    if has_human_labels(args.sheet) and not args.force:
        print(f"{args.sheet} already has human labels. Use --force to overwrite.", file=sys.stderr)
        return 1
    sheet, key = build(read_csv(args.dataset), read_csv(args.drafts), args.seed, args.total)
    write_csv(args.sheet, SHEET_FIELDS, sheet)
    write_csv(args.key, KEY_FIELDS, key)

    print(f"Wrote {len(sheet)} rows to {args.sheet} and {args.key}")
    print("By reason:", dict(Counter(k["sample_reason"].split(":")[0] for k in key)))
    print("By draft label:", dict(Counter(k["message_type"] for k in key).most_common()))
    print("By Smartlead category:", dict(Counter(r["smartlead_category"] for r in sheet)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
