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
import json
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
            f"- Words: draft {r['draft_words']} | real {r['real_words']} | cap {r['word_limit']}"
            f"  |  length fix: {r['length_fix'] or 'none'}",
            f"- Figures in our draft: {figures}  |  sentences removed for a figure: {r['removed']}"
            f"  |  price confirmed: {r['price_confirmed']}",
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
        f"Average words: draft {statistics.mean(r['draft_words'] for r in results):.0f}, "
        f"real reply {statistics.mean(r['real_words'] for r in results):.0f}; length fixes: "
        f"{sum(1 for r in results if r['length_fix'])}",
        f"Drafts with a number, price or percentage to check: "
        f"{sum(1 for r in results if r['flagged'])}",
        f"Sentences removed for a number that is not allowed: "
        f"{sum(r['removed'] for r in results)} in {sum(1 for r in results if r['removed'])} drafts",
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
    from app.examples.drafter import ExampleDrafter, word_count
    from app.examples.history import load_history, read_csv
    from app.examples.library import Example, parse_time, select_examples
    from app.examples.similarity import similarity

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)

    from app.examples.verticals import VerticalResolver
    from app.smartlead.client import SmartleadClient

    verticals = VerticalResolver(SmartleadClient.from_settings(settings).list_campaigns)
    history, _ = load_history(verticals=verticals)
    pool = [
        Example(
            e.source.value,
            e.source_thread_id,
            e.campaign_name,
            e.label,
            e.prospect_reply,
            e.gamic_reply,
            e.replied_at,
            e.sent_at,
            e.priority,
            e.vertical or "",
        )
        for e in history
    ]
    synced_file = DATA / "synced_examples.json"
    if synced_file.exists():  # written by scripts/sync_examples.py --out
        for r in json.loads(synced_file.read_text(encoding="utf-8")):
            pool.append(
                Example(
                    "smartlead_sync",
                    r["source_thread_id"],
                    r["campaign_name"],
                    r["label"],
                    r["prospect_reply"],
                    r["gamic_reply"],
                    parse_time(r["replied_at"]),
                    parse_time(r["sent_at"]),
                    0,
                    r.get("vertical") or "",
                )
            )
    sop_file = ROOT / "sop_gmc.md"  # Ray's templates (never committed)
    if sop_file.exists():
        from app.examples.sop import sop_examples

        for t in sop_examples(sop_file.read_text(encoding="utf-8-sig"))[0]:
            pool.append(
                Example(
                    t.source.value,
                    t.source_thread_id,
                    t.campaign_name,
                    t.label,
                    t.prospect_reply,
                    t.gamic_reply,
                    t.replied_at,
                    t.sent_at,
                    t.priority,
                    t.vertical or "ma",
                )
            )
    print(f"Library for the test: {len(pool)} examples ({len(history)} history)")
    all_pool = pool
    hidden: set[str] = set()  # examples hidden for the current row (its own answer, copies of it)
    drafter = ExampleDrafter(
        lambda label, vertical="": [e for e in all_pool if e.source_thread_id not in hidden],
        client,
        settings.DRAFT_MODEL,
        settings.PRICE_SENTENCE,
        vertical_of=lambda thread: str(thread.get("vertical") or ""),
    )

    data = {r["thread_id"]: r for r in read_csv(DATA / "gmc_all_replies_dataset.csv")}
    rows = pick_rows(read_csv(DATA / "gold_set.csv"), data, args.count, args.seed)

    results: list[dict[str, Any]] = []
    failed: list[str] = []
    table: list[tuple[Any, ...]] = []
    from app.smartlead.clean import clean_body

    for n, row in enumerate(rows, 1):
        print(f"[{n}/{len(rows)}] {row['gold']}", flush=True)
        thread_id = row["thread_id"]
        prospect = clean_reply(row["prospect_first_reply"])
        real = clean_body(row["gamic_response_to_first_reply"]).strip()
        first = row["lead_name"].strip().split(" ")[0]
        hidden.clear()
        hidden.update(
            e.source_thread_id
            for e in all_pool
            if e.source_thread_id == thread_id or similarity(e.gamic_reply, real) >= 90
        )
        vertical = verticals.for_campaign(int(data[thread_id]["campaign_id"]), row["campaign_name"])
        outcome = drafter.draft(
            thread={
                "campaign_name": row["campaign_name"],
                "vertical": vertical,
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
            table.append(
                (
                    len(table) + 1,
                    row["gold"],
                    vertical,
                    "-",
                    word_count(real),
                    None,
                    None,
                    outcome.reason,
                )
            )
            continue
        closest = select_examples(
            [e for e in all_pool if e.source_thread_id not in hidden],
            row["gold"],
            row["campaign_name"],
            k=1,
            reply=prospect,
            vertical=vertical,
        )
        used = [e for e in outcome.examples[:1] if e.source == "sop"]
        results.append(
            {
                "vertical": vertical,
                "first_example": "SOP" if used else "history",
                "thread_id": thread_id,
                "gold": row["gold"],
                "campaign": row["campaign_name"],
                "lead_name": row["lead_name"],
                "prospect": prospect,
                "draft": outcome.text,
                "draft_words": word_count(outcome.text),
                "real_words": word_count(real),
                "word_limit": outcome.word_limit,
                "length_fix": outcome.length_fix,
                "real": real,
                "score": similarity(outcome.text, real),
                "baseline": similarity(closest[0].gamic_reply, real) if closest else 0.0,
                "flagged": outcome.flagged,
                "removed": outcome.removed,
                "price_confirmed": outcome.price_confirmed,
                "example_ids": outcome.example_ids,
            }
        )

    if not results:
        raise SystemExit("no drafts were made")
    for r in results:
        table.append(
            (
                len(table) + 1,
                r["gold"],
                r["vertical"],
                r["draft_words"],
                r["real_words"],
                round(r["score"], 1),
                round(r["baseline"], 1),
                f"first example: {r['first_example']}"
                + (f", figures: {', '.join(r['flagged'])}" if r["flagged"] else ""),
            )
        )
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
    print("\n#  label                vertical    draft real  sim   closest  note")
    for n, label, vert, dw, rw, sim, base, note in table:
        print(f"{n:<3}{label:<21}{vert:<12}{dw!s:<6}{rw:<5}{sim!s:<6}{base!s:<9}{note}")
    for label in ("meeting_request", "price_question", "objection_info", "objection_price"):
        shown = next((r for r in results if r["gold"] == label), None)
        if shown:
            print(
                f"\n===== {label}: draft {shown['draft_words']} words, "
                f"real {shown['real_words']} words"
            )
            print(shown["prospect"][:500].strip())
            print("--- our draft")
            print(redactor(shown["lead_name"])(shown["draft"]).strip())
    print(f"\nReport: {REPORT}")


if __name__ == "__main__":
    main()
