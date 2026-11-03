"""Score the classifier against the gold set (B5).

Usage:
    python scripts/score_classifier.py                 # tuning set (the 150 rows you may look at)
    python scripts/score_classifier.py --holdout       # held-out set: run this once, at the end
    python scripts/score_classifier.py --holdout --cached-only --groups-only
                                                       # recompute from saved predictions only

Prints overall accuracy, per-label precision and recall, a confusion matrix and
do_not_contact recall. Rows marked unsure are left out of the numbers and counted.
Gate (held-out set): 90% accuracy and 99% do_not_contact recall.
A second report follows: accuracy by action group (see GROUPS) and what the confidence
threshold (CLASSIFIER_MIN_CONFIDENCE, or --min-confidence) would send to a human.
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

# Action groups: labels that lead to the same kind of next step. Used by the second report.
GROUPS: dict[str, tuple[str, ...]] = {
    "list_or_doc": ("free_list_request", "lead_magnet_request"),
    "info": ("info_request", "price_question", "price_and_info"),
    "objection": ("objection_info", "objection_price"),
    "decline": ("not_interested",),
    "meeting": ("meeting_request", "trial_request", "booked_confirmation"),
    "redirect": ("wrong_person",),
    "auto": ("out_of_office", "auto_reply_or_bounce"),
    "stop": ("do_not_contact",),
    "other": ("off_script",),
}
GROUP_OF = {label: group for group, labels in GROUPS.items() for label in labels}


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


def predict(
    rows: list[dict[str, Any]],
    classifier: Any,
    cache_path: Path,
    workers: int,
    cached_only: bool = False,
) -> dict:
    """Prediction entry per thread_id. Calls the classifier only for rows not in the cache.

    With cached_only the classifier is never called: missing rows are an error.
    """
    from app.classify.classifier import ClassifierError

    cache = load_cache(cache_path, classifier.version)
    todo = [r for r in rows if r["thread_id"] not in cache]
    if cached_only and todo:
        raise SystemExit(
            f"{len(todo)} of {len(rows)} predictions are not saved for prompt version "
            f"{classifier.version}. Run without --cached-only to classify them."
        )
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


def accuracy_summary(rows: list[dict[str, Any]], preds: dict) -> dict[str, Any]:
    """The numbers report() returns, without printing anything."""
    scored = [r for r in rows if not r["unsure"]]
    right, total = accuracy(scored, preds)
    hit, dnc = dnc_recall(scored, preds)
    return {"right": right, "total": total, "dnc_hit": hit, "dnc_total": dnc, "scored": scored}


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


def group_of(label: str) -> str:
    return GROUP_OF.get(label, ERROR)


def group_rows(rows: list[dict[str, Any]], preds: dict) -> list[dict[str, Any]]:
    """Rows with the human group and the predicted group added."""
    return [
        {
            **r,
            "human_group": group_of(r["human"]),
            "pred_group": group_of(preds[r["thread_id"]]["label"]),
        }
        for r in rows
    ]


def sent_to_human(pred: dict[str, Any], min_confidence: float) -> bool:
    """Would the confidence threshold send this prediction to human_only?"""
    from app.classify.classifier import Classification
    from app.classify.confidence import needs_human

    result = Classification(
        pred["label"], pred["confidence"], pred.get("reason", ""), pred["source"]
    )
    return pred["label"] == ERROR or needs_human(result, min_confidence)


def group_table(rows: list[dict[str, Any]]) -> list[str]:
    human = Counter(r["human_group"] for r in rows)
    pred = Counter(r["pred_group"] for r in rows)
    tp = Counter(r["human_group"] for r in rows if r["human_group"] == r["pred_group"])
    lines = [f"{'group':12} {'human':>5} {'pred':>5} {'right':>5} {'precision':>9} {'recall':>8}"]
    for group in [*GROUPS, ERROR]:
        if human[group] or pred[group]:
            lines.append(
                f"{group:12} {human[group]:>5} {pred[group]:>5} {tp[group]:>5} "
                f"{pct(tp[group], pred[group]):>9} {pct(tp[group], human[group]):>8}"
            )
    return lines


def group_confusion(rows: list[dict[str, Any]]) -> list[str]:
    cells = Counter((r["human_group"], r["pred_group"]) for r in rows)
    groups = [g for g in [*GROUPS, ERROR] if any(g in pair for pair in cells)]
    lines = [
        f"{'':12}"
        + "".join(f"{g[:8]:>9}" for g in groups)
        + "   (rows = human, columns = predicted)"
    ]
    for h in groups:
        counts = "".join(f"{cells[(h, p)] or '.':>9}" for p in groups)
        lines.append(f"{h:12}{counts}")
    return lines


def threshold_report(rows: list[dict[str, Any]], preds: dict, min_confidence: float) -> list[str]:
    """What the confidence threshold would do to these rows (rows must be the scored ones)."""
    grouped = group_rows(rows, preds)
    human = [r for r in grouped if sent_to_human(preds[r["thread_id"]], min_confidence)]
    kept = [r for r in grouped if r not in human]
    label_wrong = [r for r in grouped if preds[r["thread_id"]]["label"] != r["human"]]
    group_wrong = [r for r in grouped if r["human_group"] != r["pred_group"]]
    caught = [r for r in label_wrong if r in human]
    caught_group = [r for r in group_wrong if r in human]
    kept_label = sum(1 for r in kept if preds[r["thread_id"]]["label"] == r["human"])
    kept_group = sum(1 for r in kept if r["human_group"] == r["pred_group"])
    dnc = [r for r in grouped if r["human"] == "do_not_contact"]
    dnc_auto = [r for r in dnc if r not in human and preds[r["thread_id"]]["label"] == r["human"]]
    return [
        f"Confidence threshold {min_confidence} (rule results score 1.0 and always pass):",
        f"  sent to human_only: {len(human)} of {len(grouped)} ({pct(len(human), len(grouped))})",
        f"  label mistakes caught: {len(caught)} of {len(label_wrong)}",
        f"  group mistakes caught: {len(caught_group)} of {len(group_wrong)}",
        f"  kept (not sent to a human): {len(kept)}; label accuracy {kept_label}/{len(kept)} "
        f"({pct(kept_label, len(kept))}), group accuracy {kept_group}/{len(kept)} "
        f"({pct(kept_group, len(kept))})",
        f"  do_not_contact rows handled without a human and right: {len(dnc_auto)} of {len(dnc)}",
    ]


CLOSE_LABELS = ("not_interested", "objection_info", "objection_price")


def decision_for(pred: dict[str, Any], min_confidence: float, min_ni: float) -> Any:
    """The policy decision for one saved prediction (an error counts as no classification)."""
    from app.classify.classifier import Classification
    from app.classify.policy import decide

    if pred["label"] == ERROR:
        return decide(None, min_confidence, min_ni)
    result = Classification(
        pred["label"], pred["confidence"], pred.get("reason", ""), pred["source"]
    )
    return decide(result, min_confidence, min_ni)


def safety_report(
    rows: list[dict[str, Any]], preds: dict, min_confidence: float, min_ni: float
) -> list[str]:
    """What the safety rules do: automatic actions, and not_interested / objection handling."""
    from app.classify.policy import AUTO_SUPPRESS, SUGGEST_CLOSE

    dec = {
        r["thread_id"]: decision_for(preds[r["thread_id"]], min_confidence, min_ni) for r in rows
    }
    label = {r["thread_id"]: preds[r["thread_id"]]["label"] for r in rows}
    auto = [r for r in rows if dec[r["thread_id"]].auto_action == AUTO_SUPPRESS]
    auto_wrong = [r for r in auto if r["human"] != "do_not_contact"]

    ni = [r for r in rows if label[r["thread_id"]] == "not_interested"]
    ni_note = [r for r in ni if dec[r["thread_id"]].note == SUGGEST_CLOSE]
    ni_plain = [r for r in ni if dec[r["thread_id"]].note is None]

    def wrong(group: list[dict[str, Any]]) -> int:
        return sum(1 for r in group if r["human"] != "not_interested")

    # The earlier rule closed every not_interested prediction at or above the general threshold.
    old_closed = [r for r in ni if preds[r["thread_id"]]["confidence"] >= min_confidence]

    mine = [r for r in rows if r["human"] in CLOSE_LABELS]
    mine_closed = [r for r in mine if r in old_closed]
    by_human = Counter(r["human"] for r in mine)
    lanes = Counter(dec[r["thread_id"]].lane for r in mine)

    return [
        f"Safety rules (not_interested threshold {min_ni}; only do_not_contact acts on its own):",
        f"  automatic suppressions: {len(auto)}, wrong: {len(auto_wrong)}",
        f"  predicted not_interested: {len(ni)}, all go to a human, none are closed automatically",
        f"    with 'suggest: close' (confidence >= {min_ni}): {len(ni_note)}, "
        f"really not_interested: {len(ni_note) - wrong(ni_note)}, "
        f"something else: {wrong(ni_note)}",
        f"    human_only, no note (confidence < {min_ni}): {len(ni_plain)}, "
        f"really not_interested: {len(ni_plain) - wrong(ni_plain)}, "
        f"something else: {wrong(ni_plain)}",
        f"  before this rule, predicted not_interested at >= {min_confidence} would have been "
        f"closed automatically: {len(old_closed)}, of which not really not_interested: "
        f"{wrong(old_closed)}",
        f"  rows the human labelled not_interested/objection: {len(mine)} "
        f"({', '.join(f'{k} {v}' for k, v in sorted(by_human.items()))})",
        f"    would have been closed automatically before: {len(mine_closed)}",
        f"    now: human_only {lanes['human_only']}, "
        f"continue (draft for approval) {lanes['continue']}",
        "    closed automatically now: 0 (no action exists for these labels)",
    ]


def group_report(
    rows: list[dict[str, Any]],
    preds: dict,
    min_confidence: float,
    title: str,
    min_ni: float = 0.90,
) -> dict[str, Any]:
    """Second report: action groups, the confidence threshold, the safety rules. Unsure excluded."""
    scored = [r for r in rows if not r["unsure"]]
    grouped = group_rows(scored, preds)
    right = sum(1 for r in grouped if r["human_group"] == r["pred_group"])
    print(f"\n=== {title}: ACTION GROUPS ===")
    print(f"Scored rows: {len(grouped)} (unsure excluded: {len(rows) - len(scored)})")
    print(f"Group accuracy: {right}/{len(grouped)} = {pct(right, len(grouped))}")
    print("\n" + "\n".join(group_table(grouped)))
    print("\n" + "\n".join(group_confusion(grouped)))
    print("\n" + "\n".join(threshold_report(scored, preds, min_confidence)))
    print("\n" + "\n".join(safety_report(scored, preds, min_confidence, min_ni)))
    return {"group_right": right, "total": len(grouped)}


def log_holdout_run(path: Path, version: str, result: dict[str, Any], cached: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = (
        f"{stamp} version={version} scored={result['total']} correct={result['right']} "
        f"dnc={result['dnc_hit']}/{result['dnc_total']}{' cached=1' if cached else ''}\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--holdout", action="store_true", help="score the held-out set instead")
    p.add_argument("--mistakes", type=int, default=10, help="tuning set: how many to list")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--model", help="override the CLASSIFIER_MODEL setting")
    p.add_argument("--cached-only", action="store_true", help="never call the model")
    p.add_argument("--groups-only", action="store_true", help="print only the action-group report")
    p.add_argument("--min-confidence", type=float, help="override CLASSIFIER_MIN_CONFIDENCE")
    p.add_argument(
        "--min-confidence-not-interested",
        type=float,
        help="override CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED",
    )
    args = p.parse_args(argv)

    from app.classify.classifier import Classifier
    from app.config.settings import get_settings

    settings = get_settings()
    if args.cached_only:
        client = None  # the model is never called in this mode
    else:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)
    classifier = Classifier(client, args.model or settings.CLASSIFIER_MODEL)
    min_confidence = (
        args.min_confidence
        if args.min_confidence is not None
        else settings.CLASSIFIER_MIN_CONFIDENCE
    )

    ids = {
        line.strip() for line in HOLDOUT.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    rows = load_rows(read_csv(GOLD), ids, read_csv(SHEET), read_csv(DATASET), args.holdout)
    print(f"Model {classifier.model}, prompt version {classifier.version}")
    preds = predict(rows, classifier, CACHE, args.workers, cached_only=args.cached_only)
    title = "HELD-OUT SET" if args.holdout else "TUNING SET"

    result = accuracy_summary(rows, preds) if args.groups_only else report(rows, preds, title)
    if args.holdout:
        log_holdout_run(HOLDOUT_LOG, classifier.version, result, cached=args.cached_only)
    if not args.groups_only:
        if args.holdout:
            if not gate(result):
                print("\nGate missed. Top 10 mistakes (most confident first):")
                print("\n".join(mistakes(result["scored"], preds, 10)))
        else:
            print(f"\nTop {args.mistakes} mistakes (most confident first):")
            print("\n".join(mistakes(result["scored"], preds, args.mistakes)))
    min_ni = (
        args.min_confidence_not_interested
        if args.min_confidence_not_interested is not None
        else settings.CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED
    )
    group_report(rows, preds, min_confidence, title, min_ni)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
