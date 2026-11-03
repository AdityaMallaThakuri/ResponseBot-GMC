"""Score the classifier against the gold set (B5).

Usage:
    python scripts/score_classifier.py                 # tuning set (the 150 rows you may look at)
    python scripts/score_classifier.py --holdout       # held-out set: run this once, at the end

Prints overall accuracy, per-label precision and recall, a confusion matrix and
do_not_contact recall. Rows marked unsure are left out of the numbers and counted.
Gate (held-out set): 90% accuracy and 99% do_not_contact recall.
Predictions are cached in eval/data/classifier_cache.jsonl, keyed by thread_id and prompt
version, so reruns only call the API for rows that are new or whose prompt changed.
Every held-out run is appended to eval/reports/holdout_runs.log so peeking leaves a trail.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "eval" / "data"
GOLD = DATA / "gold_set.csv"
HOLDOUT = DATA / "holdout_ids.txt"
SHEET = DATA / "review_sheet.csv"
DATASET = DATA / "gmc_all_replies_dataset.csv"
CACHE = DATA / "classifier_cache.jsonl"
HOLDOUT_LOG = ROOT / "eval" / "reports" / "holdout_runs.log"

GATE_ACCURACY = 0.90
GATE_DNC_RECALL = 0.99
UNSURE_MARKS = {"Y", "YES", "1", "TRUE", "X"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ERROR = "(error)"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def load_rows(
    gold: list[dict[str, str]],
    holdout_ids: set[str],
    sheet: list[dict[str, str]],
    dataset: list[dict[str, str]],
    holdout: bool,
) -> list[dict[str, Any]]:
    """Gold rows of the chosen set, joined with the text we classify and the unsure mark."""
    unsure = {r["thread_id"]: r["unsure"].strip().upper() in UNSURE_MARKS for r in sheet}
    data = {r["thread_id"]: r for r in dataset}
    rows = []
    for g in gold:
        if (g["thread_id"] in holdout_ids) != holdout:
            continue
        d = data[g["thread_id"]]
        rows.append(
            {
                "thread_id": g["thread_id"],
                "human": g["human_message_type"].strip(),
                "unsure": unsure.get(g["thread_id"], False),
                "reply": d["prospect_first_reply"],
                "campaign": d["campaign_name"],
                "outreach": d["outreach_email"],
            }
        )
    return rows


# --------------------------------------------------------------------------- predictions


def load_cache(path: Path, version: str) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                if entry.get("version") == version:
                    cache[entry["thread_id"]] = entry
    return cache


def predict(rows: list[dict[str, Any]], classifier: Any, cache_path: Path, workers: int) -> dict:
    """Prediction entry per thread_id. Calls the classifier only for rows not in the cache."""
    from app.classify.classifier import ClassifierError

    cache = load_cache(cache_path, classifier.version)
    todo = [r for r in rows if r["thread_id"] not in cache]
    lock = threading.Lock()

    def work(row: dict[str, Any]) -> None:
        base = {"thread_id": row["thread_id"], "version": classifier.version}
        try:
            c = classifier.classify(row["reply"], row["campaign"], row["outreach"])
            entry = {**base, "label": c.label, "confidence": c.confidence}
            entry |= {"reason": c.reason, "source": c.source, "usage": c.usage}
        except ClassifierError as exc:
            entry = {**base, "label": ERROR, "confidence": 0.0, "reason": str(exc), "source": "llm"}
        cache[row["thread_id"]] = entry
        with lock, cache_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, todo))
    print(f"{len(rows)} rows: {len(rows) - len(todo)} cached, {len(todo)} classified now.")
    return cache


# --------------------------------------------------------------------------- metrics


def pct(n: int, total: int) -> str:
    return f"{n / total * 100:.1f}%" if total else "n/a"


def accuracy(rows: list[dict[str, Any]], preds: dict) -> tuple[int, int]:
    return sum(1 for r in rows if preds[r["thread_id"]]["label"] == r["human"]), len(rows)


def dnc_recall(rows: list[dict[str, Any]], preds: dict) -> tuple[int, int]:
    dnc = [r for r in rows if r["human"] == "do_not_contact"]
    hit = sum(1 for r in dnc if preds[r["thread_id"]]["label"] == "do_not_contact")
    return hit, len(dnc)


def label_table(rows: list[dict[str, Any]], preds: dict) -> list[str]:
    human = Counter(r["human"] for r in rows)
    pred = Counter(preds[r["thread_id"]]["label"] for r in rows)
    tp = Counter(r["human"] for r in rows if preds[r["thread_id"]]["label"] == r["human"])
    head = f"{'label':22} {'human':>5} {'pred':>5} {'right':>5} {'precision':>9} {'recall':>8}"
    lines = [head]
    for label in sorted(set(human) | set(pred)):
        lines.append(
            f"{label:22} {human[label]:>5} {pred[label]:>5} {tp[label]:>5} "
            f"{pct(tp[label], pred[label]):>9} {pct(tp[label], human[label]):>8}"
        )
    return lines


def confusion_matrix(rows: list[dict[str, Any]], preds: dict) -> list[str]:
    cells = Counter((r["human"], preds[r["thread_id"]]["label"]) for r in rows)
    labels = sorted({h for h, _ in cells} | {p for _, p in cells})
    index = {label: i + 1 for i, label in enumerate(labels)}
    lines = ["Confusion matrix (rows = human label, columns = predicted):"]
    lines += [f"  {i}: {label}" for label, i in index.items()]
    lines.append(f"{'':4}" + "".join(f"{i:>4}" for i in index.values()))
    for h in labels:
        counts = "".join(f"{cells[(h, p)] or '.':>4}" for p in labels)
        lines.append(f"{index[h]:>3} {counts}")
    return lines


def rule_report(rows: list[dict[str, Any]], preds: dict) -> list[str]:
    by_rule = [r for r in rows if preds[r["thread_id"]]["source"] == "rule"]
    right, total = accuracy(by_rule, preds)
    llm = [r for r in rows if preds[r["thread_id"]]["source"] != "rule"]
    r2, t2 = accuracy(llm, preds)
    return [
        f"Handled by rules: {total} rows, {right} right ({pct(right, total)})",
        f"Handled by the model: {t2} rows, {r2} right ({pct(r2, t2)})",
    ]


def mask(text: str) -> str:
    return EMAIL_RE.sub("[email]", text)


def mistakes(rows: list[dict[str, Any]], preds: dict, n: int) -> list[str]:
    """The n most confident wrong answers, with the model's reason and a short masked excerpt."""
    wrong = [r for r in rows if preds[r["thread_id"]]["label"] != r["human"]]
    wrong.sort(key=lambda r: (-preds[r["thread_id"]]["confidence"], r["thread_id"]))
    lines = []
    for r in wrong[:n]:
        p = preds[r["thread_id"]]
        excerpt = " ".join(mask(r["reply"]).split())[:80]
        lines.append(
            f"{r['thread_id']} | human {r['human']} | predicted {p['label']} "
            f"({p['source']}, {p['confidence']:.2f})\n"
            f"    reason: {mask(p['reason'])[:160]}\n    reply: {excerpt!r}"
        )
    return lines


def report(rows: list[dict[str, Any]], preds: dict, title: str) -> dict[str, Any]:
    """Print the full report for one set. Unsure rows are excluded from every number."""
    scored = [r for r in rows if not r["unsure"]]
    unsure = len(rows) - len(scored)
    right, total = accuracy(scored, preds)
    hit, dnc = dnc_recall(scored, preds)
    all_right, all_total = accuracy(rows, preds)
    print(f"\n=== {title} ===")
    print(f"Rows: {len(rows)}; marked unsure and excluded: {unsure}; scored: {total}")
    print(f"Accuracy: {right}/{total} = {pct(right, total)}")
    print(f"(including unsure rows: {all_right}/{all_total} = {pct(all_right, all_total)})")
    print(f"do_not_contact recall: {hit}/{dnc} = {pct(hit, dnc)}")
    print("\n".join(rule_report(scored, preds)))
    print("\n" + "\n".join(label_table(scored, preds)))
    print("\n" + "\n".join(confusion_matrix(scored, preds)))
    return {"right": right, "total": total, "dnc_hit": hit, "dnc_total": dnc, "scored": scored}


def gate(result: dict[str, Any]) -> bool:
    acc = result["right"] / result["total"] if result["total"] else 0.0
    recall = result["dnc_hit"] / result["dnc_total"] if result["dnc_total"] else 0.0
    ok_acc, ok_dnc = acc >= GATE_ACCURACY, recall >= GATE_DNC_RECALL
    print(f"\nGate accuracy >= {GATE_ACCURACY:.0%}: {'PASS' if ok_acc else 'MISS'} ({acc:.1%})")
    ok_text = "PASS" if ok_dnc else "MISS"
    print(f"Gate do_not_contact recall >= {GATE_DNC_RECALL:.0%}: {ok_text} ({recall:.1%})")
    return ok_acc and ok_dnc


def log_holdout_run(path: Path, version: str, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = (
        f"{stamp} version={version} scored={result['total']} correct={result['right']} "
        f"dnc={result['dnc_hit']}/{result['dnc_total']}\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--holdout", action="store_true", help="score the held-out set instead")
    p.add_argument("--mistakes", type=int, default=10, help="tuning set: how many to list")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--model", help="override the CLASSIFIER_MODEL setting")
    args = p.parse_args(argv)

    import anthropic

    from app.classify.classifier import Classifier
    from app.config.settings import get_settings

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)
    classifier = Classifier(client, args.model or settings.CLASSIFIER_MODEL)

    ids = {
        line.strip() for line in HOLDOUT.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    rows = load_rows(read_csv(GOLD), ids, read_csv(SHEET), read_csv(DATASET), args.holdout)
    print(f"Model {classifier.model}, prompt version {classifier.version}")
    preds = predict(rows, classifier, CACHE, args.workers)

    result = report(rows, preds, "HELD-OUT SET" if args.holdout else "TUNING SET")
    if args.holdout:
        log_holdout_run(HOLDOUT_LOG, classifier.version, result)
        if not gate(result):
            print("\nGate missed. Top 10 mistakes (most confident first):")
            print("\n".join(mistakes(result["scored"], preds, 10)))
    else:
        print(f"\nTop {args.mistakes} mistakes (most confident first):")
        print("\n".join(mistakes(result["scored"], preds, args.mistakes)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
