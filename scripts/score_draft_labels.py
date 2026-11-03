"""Score the draft labels against a human-filled review sheet (B3).

Usage: python scripts/score_draft_labels.py --labeler <name>

Reads eval/data/review_sheet.csv (human columns filled in) and eval/data/review_key.csv.
Prints agreement for message_type, lead_verdict and playbook_template, plus the disagreements
(thread_id and labels only, never message text). Saves the filled rows as eval/data/gold_set.csv.
The sample is stratified, so agreement here is not the accuracy over all threads.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHEET = ROOT / "eval" / "data" / "review_sheet.csv"
KEY = ROOT / "eval" / "data" / "review_key.csv"
GOLD = ROOT / "eval" / "data" / "gold_set.csv"
LABELS_YAML = ROOT / "app" / "config" / "labels.yaml"

GOLD_FIELDS = [
    "thread_id", "human_message_type", "human_verdict", "human_template", "labeler", "notes",
]  # fmt: skip


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def allowed_values(config: dict[str, Any]) -> dict[str, set[str]]:
    return {
        "human_message_type": set(config["labels"]),
        "human_verdict": set(config["verdicts"]),
        "human_template": {"none", *config["templates"]},
    }


def norm_template(value: str) -> str:
    return value.strip() or "none"


def join_rows(sheet: list[dict[str, str]], key: list[dict[str, str]]) -> list[dict[str, str]]:
    """Sheet rows with their draft labels, keyed by thread_id."""
    drafts = {k["thread_id"]: k for k in key}
    joined = []
    for row in sheet:
        draft = drafts.get(row["thread_id"])
        if draft is None:
            raise SystemExit(f"thread_id {row['thread_id']} is in the sheet but not in the key")
        joined.append({**row, **{f"draft_{k}": v for k, v in draft.items() if k != "thread_id"}})
    return joined


def check_values(rows: list[dict[str, str]], allowed: dict[str, set[str]]) -> list[str]:
    """Problems with the filled-in values, one line each. Empty list means all valid."""
    problems = []
    for r in rows:
        for field, ok in allowed.items():
            value = r[field].strip()
            if field == "human_template":
                value = norm_template(value)
            elif not value:
                continue
            if value not in ok:
                problems.append(f"{r['thread_id']}: {field} = {value!r} is not allowed")
    return problems


def is_complete(r: dict[str, str]) -> bool:
    return bool(r["human_message_type"].strip() and r["human_verdict"].strip())


UNSURE_MARKS = {"Y", "YES", "1", "TRUE", "X"}


def is_unsure(row: dict[str, str]) -> bool:
    return row["unsure"].strip().upper() in UNSURE_MARKS


def pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "n/a"


def agreement(rows: list[dict[str, str]], draft: str, human: str) -> tuple[int, int]:
    """(agreed, total) for one field pair. Templates compare with blank treated as none."""
    agreed = sum(1 for r in rows if _val(r, draft) == _val(r, human))
    return agreed, len(rows)


def _val(row: dict[str, str], field: str) -> str:
    value = row[field].strip()
    return norm_template(value) if "template" in field else value


def label_table(rows: list[dict[str, str]]) -> list[str]:
    draft_n = Counter(r["draft_message_type"] for r in rows)
    human_n = Counter(r["human_message_type"].strip() for r in rows)
    both = Counter(
        r["draft_message_type"]
        for r in rows
        if r["draft_message_type"] == r["human_message_type"].strip()
    )
    lines = [f"{'label':22} {'draft':>5} {'human':>5} {'agree':>5} {'of draft':>9} {'of human':>9}"]
    for label in sorted(set(draft_n) | set(human_n)):
        lines.append(
            f"{label:22} {draft_n[label]:>5} {human_n[label]:>5} {both[label]:>5} "
            f"{pct(both[label], draft_n[label]):>9} {pct(both[label], human_n[label]):>9}"
        )
    return lines


def field_report(rows: list[dict[str, str]], name: str, draft: str, human: str) -> list[str]:
    agreed, total = agreement(rows, draft, human)
    lines = [f"{name}: {agreed}/{total} agree ({pct(agreed, total)})"]
    unsure = [r for r in rows if not is_unsure(r)]
    a2, t2 = agreement(unsure, draft, human)
    lines.append(f"  excluding rows marked unsure: {a2}/{t2} ({pct(a2, t2)})")
    pairs = Counter(
        (_val(r, draft), _val(r, human)) for r in rows if _val(r, draft) != _val(r, human)
    )
    for (d, h), n in pairs.most_common():
        lines.append(f"  draft {d} -> human {h}: {n}")
    return lines


def confusion_list(rows: list[dict[str, str]]) -> list[str]:
    lines = [f"{'thread_id':12} {'draft':22} human"]
    for r in rows:
        if r["draft_message_type"] != r["human_message_type"].strip():
            lines.append(
                f"{r['thread_id']:12} {r['draft_message_type']:22} {r['human_message_type']}"
            )
    return lines


def gold_rows(rows: list[dict[str, str]], labeler: str) -> list[dict[str, str]]:
    return [
        {
            "thread_id": r["thread_id"],
            "human_message_type": r["human_message_type"].strip(),
            "human_verdict": r["human_verdict"].strip(),
            "human_template": norm_template(r["human_template"]),
            "labeler": labeler,
            "notes": r["notes"].strip(),
        }
        for r in rows
    ]


def write_gold(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=GOLD_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--labeler", required=True, help="who filled in the sheet")
    p.add_argument("--sheet", type=Path, default=SHEET)
    p.add_argument("--key", type=Path, default=KEY)
    p.add_argument("--gold", type=Path, default=GOLD)
    args = p.parse_args(argv)

    config = yaml.safe_load(LABELS_YAML.read_text(encoding="utf-8"))
    joined = join_rows(read_csv(args.sheet), read_csv(args.key))
    problems = check_values(joined, allowed_values(config))
    if problems:
        print("Fix these values in the sheet first:")
        print("\n".join(f"  {line}" for line in problems))
        return 1

    filled = [r for r in joined if is_complete(r)]
    started = sum(1 for r in joined if r["human_message_type"].strip())
    print(f"{len(filled)} of {len(joined)} rows are complete (label and verdict).")
    if started > len(filled):
        print(f"  {started - len(filled)} rows have a label but no verdict and are skipped.")
    if not filled:
        return 1
    print(f"Marked unsure: {sum(1 for r in filled if is_unsure(r))}")
    print("Sample is stratified, so these figures are not accuracy over all threads.\n")

    print(
        "\n".join(field_report(filled, "message_type", "draft_message_type", "human_message_type"))
    )
    print("\nPer label:")
    print("\n".join(label_table(filled)))
    print("\nmessage_type disagreements:")
    print("\n".join(confusion_list(filled)))
    print()
    print("\n".join(field_report(filled, "lead_verdict", "draft_lead_verdict", "human_verdict")))
    print()
    print(
        "\n".join(
            field_report(filled, "playbook_template", "draft_playbook_template", "human_template")
        )
    )

    write_gold(args.gold, gold_rows(filled, args.labeler))
    print(f"\nSaved {len(filled)} rows to {args.gold}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
