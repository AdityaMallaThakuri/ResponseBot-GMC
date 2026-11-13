"""Build the history examples from the past replies dataset (eval/data, never committed).

One example per thread that has both a prospect first reply and a Gamic response to it:
the prospect's first reply (cleaned), Gamic's response (cleaned), campaign name, label and date.
The label is the human gold label if the thread has one, else the human label from the draft
labels file, else the model label from that file.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.db.records import ExampleSource, NewExample
from app.examples.library import parse_time
from app.smartlead.clean import clean_body

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "eval" / "data"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def label_map(gold: list[dict[str, str]], draft_labels: list[dict[str, str]]) -> dict[str, str]:
    """thread_id -> label. Gold beats the human draft label, which beats the model's."""
    labels: dict[str, str] = {}
    for row in draft_labels:
        label = (row.get("human_message_type") or row.get("message_type") or "").strip()
        if label:
            labels[row["thread_id"]] = label
    for row in gold:
        if row.get("human_message_type", "").strip():
            labels[row["thread_id"]] = row["human_message_type"].strip()
    return labels


def row_vertical(row: dict[str, str], verticals: Any | None) -> str | None:
    """The vertical of a dataset row, through its campaign id so a POS REPLY row takes its
    parent's. None without a resolver."""
    if verticals is None:
        return None
    raw = (row.get("campaign_id") or "").strip()
    campaign_id = int(raw) if raw.isdigit() else None
    return verticals.for_campaign(campaign_id, row.get("campaign_name") or "")


def history_examples(
    dataset: list[dict[str, str]], labels: dict[str, str], verticals: Any | None = None
) -> tuple[list[NewExample], int]:
    """(examples, number of dataset rows skipped for a missing text or label)."""
    out: list[NewExample] = []
    skipped = 0
    for row in dataset:
        thread_id = row["thread_id"]
        prospect = clean_body(row.get("prospect_first_reply") or "").strip()
        gamic = clean_body(row.get("gamic_response_to_first_reply") or "").strip()
        label = labels.get(thread_id)
        if not (prospect and gamic and label):
            skipped += 1
            continue
        out.append(
            NewExample(
                source=ExampleSource.HISTORY,
                source_thread_id=thread_id,
                label=label,
                prospect_reply=prospect,
                gamic_reply=gamic,
                campaign_name=row.get("campaign_name") or "",
                replied_at=parse_time(row.get("first_reply_time")),
                vertical=row_vertical(row, verticals),
            )
        )
    return out, skipped


def load_history(
    data_dir: Path = DATA, verticals: Any | None = None
) -> tuple[list[NewExample], int]:
    dataset = read_csv(data_dir / "gmc_all_replies_dataset.csv")
    labels = label_map(read_csv(data_dir / "gold_set.csv"), read_csv(data_dir / "draft_labels.csv"))
    return history_examples(dataset, labels, verticals)
