"""Test example-based drafting on the past: our draft against what Gamic really sent.

Usage:
    python scripts/draft_vs_real.py [--count 30] [--seed 7]

Takes a stratified sample of gold_set.csv (only replies Gamic actually answered). For each, the
draft is written WITHOUT that thread's own answer in the example pool, using the human gold label,
then compared with the real reply (similarity 0 to 100, see app/examples/similarity.py). As a
yardstick it also scores "just send the closest example unchanged".

Writes eval/reports/draft_vs_real.md with lead names and emails redacted. It calls only the
Anthropic API: never Smartlead, never Slack, never the database, nothing is sent.
"""

from __future__ import annotations

import argparse
import random
import re
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "eval" / "data"
REPORT = ROOT / "eval" / "reports" / "draft_vs_real.md"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
REPLY_CHARS = 600

# Rows per gold label (30 in all); a short label is topped up from the others.
QUOTAS = {
    "free_list_request": 5,
    "info_request": 5,
    "lead_magnet_request": 5,
    "price_question": 4,
    "meeting_request": 4,
    "price_and_info": 2,
    "objection_info": 2,
    "booked_confirmation": 2,
    "trial_request": 1,
}


def redactor(lead_name: str):
    """Hides the lead's name parts and any email address."""
    parts = [p for p in re.split(r"\s+", lead_name or "") if len(p) >= 2 and p.isalpha()]
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, parts)) + r")\b", re.I) if parts else None

    def redact(text: str) -> str:
        text = EMAIL_RE.sub("[email]", text)
        return pattern.sub("[NAME]", text) if pattern else text

    return redact


def pick_rows(
    gold: list[dict[str, str]], data: dict[str, dict[str, str]], count: int, seed: int
) -> list[dict[str, str]]:
    rng = random.Random(seed)
    pools: dict[str, list[dict[str, str]]] = {label: [] for label in QUOTAS}
    for g in sorted(gold, key=lambda g: g["thread_id"]):
        row = data.get(g["thread_id"])
        label = g["human_message_type"].strip()
        if row and label in pools and row["gamic_response_to_first_reply"].strip():
            pools[label].append(row)
    chosen: list[dict[str, str]] = []
    for label, quota in QUOTAS.items():
        rng.shuffle(pools[label])
        chosen += [{**r, "gold": label} for r in pools[label][:quota]]
        pools[label] = pools[label][quota:]
    leftovers = [{**r, "gold": label} for label, rows in pools.items() for r in rows]
    rng.shuffle(leftovers)
    return (chosen + leftovers)[:count]


def block(i: int, r: dict[str, Any]) -> str:
    redact = redactor(r["lead_name"])
    reply = redact(r["prospect"])[:REPLY_CHARS]
    figures = ", ".join(r["flagged"]) or "none"
    return "\n".join(
        [
            f"## {i}. {r['campaign']}",
            "",
            f"- Thread: {r['thread_id']}  |  label: {r['gold']}  |  "
            f"**similarity {r['score']:.1f}**  |  closest example unchanged: {r['baseline']:.1f}",
            f"- Examples used: {', '.join(r['example_ids'])}",
            f"- Figures in our draft: {figures}",
            "",
            "**Prospect reply**",
            "",
            "```text",
            reply.strip(),
            "```",
            "",
            "**Our draft**",
            "",
            "```text",
            redact(r["draft"]).strip(),
            "```",
            "",
            "**What Gamic really sent**",
            "",
            "```text",
            redact(r["real"]).strip(),
            "```",
            "",
        ]
    )


def summary(results: list[dict[str, Any]], failed: list[str]) -> list[str]:
    scores = [r["score"] for r in results]
    base = [r["baseline"] for r in results]
    lines = [
        f"Replies drafted: {len(results)} (no draft: {len(failed)})",
        f"Average similarity, our draft vs the real reply: {statistics.mean(scores):.1f}"
        f" (median {statistics.median(scores):.1f}, lowest {min(scores):.1f}, "
        f"highest {max(scores):.1f})",
        f"Yardstick, the closest example sent unchanged: average {statistics.mean(base):.1f}",
        f"Drafts with a number, price or percentage to check: "
        f"{sum(1 for r in results if r['flagged'])}",
    ]
    by_label: dict[str, list[float]] = {}
    for r in results:
        by_label.setdefault(r["gold"], []).append(r["score"])
    lines += [
        f"  {label}: {statistics.mean(v):.1f} over {len(v)}"
        for label, v in sorted(by_label.items())
    ]
    lines += [f"No draft: {item}" for item in failed]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    import anthropic

    from app.classify.classifier import clean_reply
    from app.config.settings import get_settings
    from app.examples.drafter import ExampleDrafter
    from app.examples.history import load_history, read_csv
    from app.examples.library import Example, select_examples
    from app.examples.similarity import similarity

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)

    history, _ = load_history()
    pool = [
        Example(
            e.source.value,
            e.source_thread_id,
            e.campaign_name,
            e.label,
            e.prospect_reply,
            e.gamic_reply,
            e.replied_at,
        )
        for e in history
    ]
    drafter = ExampleDrafter(lambda label: pool, client, settings.DRAFT_MODEL)

    data = {r["thread_id"]: r for r in read_csv(DATA / "gmc_all_replies_dataset.csv")}
    rows = pick_rows(read_csv(DATA / "gold_set.csv"), data, args.count, args.seed)

    results: list[dict[str, Any]] = []
    failed: list[str] = []
    from app.smartlead.clean import clean_body

    for n, row in enumerate(rows, 1):
        print(f"[{n}/{len(rows)}] {row['gold']}", flush=True)
        thread_id = row["thread_id"]
        prospect = clean_reply(row["prospect_first_reply"])
        real = clean_body(row["gamic_response_to_first_reply"]).strip()
        first = row["lead_name"].strip().split(" ")[0]
        outcome = drafter.draft(
            thread={
                "campaign_name": row["campaign_name"],
                "lead_first_name": first if first.isalpha() else "",
            },
            label=row["gold"],
            confidence=1.0,
            reply=prospect,
            outreach=row["outreach_email"],
            min_confidence=0.0,
            exclude_thread_id=thread_id,  # the thread's own answer is never an example
        )
        if outcome.kind != "drafted" or not outcome.text:
            failed.append(f"{thread_id} ({outcome.reason})")
            continue
        closest = select_examples(
            pool, row["gold"], row["campaign_name"], k=1, exclude_thread_id=thread_id
        )
        results.append(
            {
                "thread_id": thread_id,
                "gold": row["gold"],
                "campaign": row["campaign_name"],
                "lead_name": row["lead_name"],
                "prospect": prospect,
                "draft": outcome.text,
                "real": real,
                "score": similarity(outcome.text, real),
                "baseline": similarity(closest[0].gamic_reply, real) if closest else 0.0,
                "flagged": outcome.flagged,
                "example_ids": outcome.example_ids,
            }
        )

    if not results:
        raise SystemExit("no drafts were made")
    stats = summary(results, failed)
    header = [
        "# Draft vs real",
        "",
        "Example-based drafts for past replies, written without the thread's own answer, against "
        "what Gamic really sent. Nothing was sent, posted or stored. Names and emails are "
        "redacted. Similarity: 0 to 100 (app/examples/similarity.py).",
        "",
        "## Summary",
        "",
        *[f"- {s}" if not s.startswith("  ") else s for s in stats],
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    body = [block(i, r) for i, r in enumerate(results, 1)]
    REPORT.write_text("\n".join(header) + "\n" + "\n".join(body), encoding="utf-8")
    print("\n" + "\n".join(stats))
    print(f"\nReport: {REPORT}")


if __name__ == "__main__":
    main()
