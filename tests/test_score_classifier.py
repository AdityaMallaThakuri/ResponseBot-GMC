"""Tests for scripts/split_gold.py and scripts/score_classifier.py. Synthetic data only."""

import csv
import random
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import score_classifier as sc  # noqa: E402
import split_gold as sg  # noqa: E402


def gold_rows() -> list[dict[str, str]]:
    sizes = {"info_request": 40, "do_not_contact": 25, "not_interested": 30, "trial_request": 3}
    sizes |= {"wrong_person": 22, "off_script": 14, "meeting_request": 5, "out_of_office": 16}
    return [
        {"thread_id": f"{label}-{i}", "human_message_type": label}
        for label, n in sizes.items()
        for i in range(n)
    ]


def test_split_is_stratified_fixed_seed_and_the_right_size() -> None:
    rows = gold_rows()
    ids = sg.split(rows, 50, seed=7)
    assert len(ids) == len(set(ids)) == 50
    assert ids == sg.split(rows, 50, seed=7)
    assert ids != sg.split(rows, 50, seed=8)
    held = Counter(i.rsplit("-", 1)[0] for i in ids)
    assert held["info_request"] in (12, 13) and held["do_not_contact"] in (8, 9)
    assert all(held[label] >= 1 for label in ("trial_request", "meeting_request"))


def test_allocate_never_exceeds_a_label_count() -> None:
    alloc = sg.allocate({"a": 1, "b": 1, "c": 98}, 50, random.Random(1))
    assert sum(alloc.values()) == 50 and alloc["a"] <= 1 and alloc["b"] <= 1


def test_split_main_refuses_to_resplit(tmp_path: Path) -> None:
    gold, out = tmp_path / "gold.csv", tmp_path / "ids.txt"
    with gold.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["thread_id", "human_message_type"])
        writer.writeheader()
        writer.writerows(gold_rows())
    args = ["--gold", str(gold), "--out", str(out)]
    assert sg.main(args) == 0
    first = out.read_text()
    assert sg.main(args) == 1 and out.read_text() == first
    assert sg.main([*args, "--force"]) == 0


def pred(label: str, conf: float = 0.9, source: str = "llm", reason: str = "r") -> dict:
    return {"label": label, "confidence": conf, "source": source, "reason": reason}


def row(tid: str, human: str, unsure: bool = False, reply: str = "text") -> dict:
    return {"thread_id": tid, "human": human, "unsure": unsure, "reply": reply}


def test_metrics_exclude_unsure_and_report_dnc_recall(capsys: pytest.CaptureFixture[str]) -> None:
    rows = [
        row("a", "do_not_contact"),
        row("b", "do_not_contact"),
        row("c", "info_request"),
        row("d", "info_request", unsure=True),  # would be wrong, but is excluded
    ]
    preds = {
        "a": pred("do_not_contact", source="rule"),
        "b": pred("not_interested"),
        "c": pred("info_request"),
        "d": pred("off_script"),
    }
    result = sc.report(rows, preds, "TEST")
    out = capsys.readouterr().out
    assert (result["right"], result["total"]) == (2, 3)
    assert (result["dnc_hit"], result["dnc_total"]) == (1, 2)
    assert "marked unsure and excluded: 1; scored: 3" in out
    assert "Accuracy: 2/3 = 66.7%" in out and "do_not_contact recall: 1/2 = 50.0%" in out
    assert "Handled by rules: 1 rows, 1 right (100.0%)" in out
    assert "(including unsure rows: 2/4 = 50.0%)" in out


def test_label_table_and_confusion_matrix() -> None:
    rows = [row("a", "x"), row("b", "x"), row("c", "y")]
    preds = {"a": pred("x"), "b": pred("y"), "c": pred("y")}
    table = "\n".join(sc.label_table(rows, preds))
    assert "50.0%" in table and "100.0%" in table
    matrix = sc.confusion_matrix(rows, preds)
    assert matrix[0].startswith("Confusion matrix")
    assert any(line.strip().startswith("1") and "1" in line for line in matrix)


def test_gate_needs_both_thresholds() -> None:
    ok = {"right": 46, "total": 50, "dnc_hit": 6, "dnc_total": 6}
    assert sc.gate(ok)
    assert not sc.gate({**ok, "right": 44})
    assert not sc.gate({**ok, "dnc_hit": 5})


def test_mistakes_are_most_confident_first_with_masked_excerpts() -> None:
    rows = [
        row("a", "x", reply="mail bob@x.com please"),
        row("b", "x"),
        row("c", "x"),
    ]
    preds = {"a": pred("y", 0.6), "b": pred("y", 0.95, reason="r bob@x.com"), "c": pred("x")}
    lines = sc.mistakes(rows, preds, 10)
    assert len(lines) == 2 and lines[0].startswith("b |")
    assert "bob@x.com" not in "".join(lines) and "[email]" in "".join(lines)


def test_load_rows_picks_the_requested_set_and_unsure_marks() -> None:
    gold = [
        {"thread_id": "t1", "human_message_type": "info_request"},
        {"thread_id": "t2", "human_message_type": "off_script"},
    ]
    sheet = [{"thread_id": "t1", "unsure": ""}, {"thread_id": "t2", "unsure": "1"}]
    dataset = [
        {"thread_id": t, "prospect_first_reply": "r", "campaign_name": "c", "outreach_email": "o"}
        for t in ("t1", "t2")
    ]
    tuning = sc.load_rows(gold, {"t2"}, sheet, dataset, holdout=False)
    held = sc.load_rows(gold, {"t2"}, sheet, dataset, holdout=True)
    assert [r["thread_id"] for r in tuning] == ["t1"] and not tuning[0]["unsure"]
    assert [r["thread_id"] for r in held] == ["t2"] and held[0]["unsure"]


def test_predict_uses_the_cache_and_records_errors(tmp_path: Path) -> None:
    from app.classify.classifier import Classification, ClassifierError

    calls: list[str] = []

    def classify(reply: str, campaign: str, outreach: str) -> Classification:
        calls.append(reply)
        if reply == "bad":
            raise ClassifierError("boom")
        return Classification("info_request", 0.8, "ok", "llm")

    clf = SimpleNamespace(version="v1", classify=classify)
    rows = [
        {**row("a", "x"), "reply": "good", "campaign": "c", "outreach": "o"},
        {**row("b", "x"), "reply": "bad", "campaign": "c", "outreach": "o"},
    ]
    cache = tmp_path / "cache.jsonl"
    first = sc.predict(rows, clf, cache, workers=1)
    assert first["a"]["label"] == "info_request" and first["b"]["label"] == sc.ERROR
    sc.predict(rows, clf, cache, workers=1)
    assert len(calls) == 2  # second run came from the cache
    sc.predict(rows, SimpleNamespace(version="v2", classify=classify), cache, workers=1)
    assert len(calls) == 4  # a new prompt version recomputes


def test_holdout_run_is_logged(tmp_path: Path) -> None:
    log = tmp_path / "reports" / "holdout_runs.log"
    sc.log_holdout_run(log, "v1", {"total": 50, "right": 46, "dnc_hit": 6, "dnc_total": 6})
    sc.log_holdout_run(log, "v2", {"total": 50, "right": 47, "dnc_hit": 6, "dnc_total": 6})
    lines = log.read_text().splitlines()
    assert len(lines) == 2 and "version=v1" in lines[0] and "dnc=6/6" in lines[1]
