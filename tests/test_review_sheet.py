"""Tests for make_review_sheet.py and score_draft_labels.py. Small fake files only."""

import csv
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import make_review_sheet as mk  # noqa: E402
import score_draft_labels as sc  # noqa: E402


def draft(
    i: int, label: str, category: str = "Not Interested", conf: float = 0.9
) -> dict[str, str]:
    return {
        "thread_id": f"{label}-{category[:3]}-{i:03d}",
        "category_name": category,
        "message_type": label,
        "lead_verdict": "pass",
        "playbook_template": "none",
        "confidence": str(conf),
        "status": "ok",
    }


def synthetic_drafts() -> list[dict[str, str]]:
    rows = [draft(i, "trial_request") for i in range(2)]
    rows += [draft(i, "objection_info") for i in range(2)]
    rows += [draft(i, "objection_price") for i in range(3)]
    rows += [draft(i, "booked_confirmation") for i in range(3)]
    rows += [draft(i, "do_not_contact", "Do Not Contact") for i in range(20)]
    rows += [draft(i + 20, "do_not_contact", "Not Interested") for i in range(20)]
    rows += [draft(i, "wrong_person", "Out Of Office") for i in range(10)]
    rows += [draft(i + 10, "wrong_person", "Do Not Contact") for i in range(10)]
    rows += [draft(i + 20, "wrong_person", "Wrong Person") for i in range(10)]
    rows += [draft(i, "off_script") for i in range(25)]
    for label in ("info_request", "not_interested", "out_of_office", "meeting_request"):
        rows += [draft(i, label) for i in range(30)]
    # distinct low confidences on a few rows
    for n, row in enumerate(r for r in rows if r["message_type"] == "info_request"):
        if n < 5:
            row["confidence"] = str(0.1 + n / 100)
    return rows


def dataset_for(drafts: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "thread_id": d["thread_id"],
            "campaign_name": "GMC - Test",
            "category_name": d["category_name"],
            "outreach_email": "Hi Sam, write to sam@firm.com. " + "o" * 500,
            "prospect_first_reply": "Call me at sam@firm.com " + "r" * 1000,
            "gamic_response_to_first_reply": "g" * 600,
        }
        for d in drafts
    ]


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_sample_hits_every_quota() -> None:
    drafts = mk.usable_drafts(synthetic_drafts())
    picked = mk.sample_threads(drafts, seed=7, total=120)
    by_id = {d["thread_id"]: d for d in drafts}
    labels = Counter(by_id[t]["message_type"] for t in picked)
    reasons = Counter(r.split(":")[0] for r in picked.values())
    assert len(picked) == 120
    for rare, n in (("trial_request", 2), ("objection_info", 2), ("objection_price", 3)):
        assert labels[rare] == n
    assert labels["booked_confirmation"] == 3
    dnc = [t for t, r in picked.items() if r.startswith("do_not_contact")]
    assert len(dnc) == 25
    assert sum(1 for t in dnc if by_id[t]["category_name"] != "Do Not Contact") >= 10
    wrong = [t for t, r in picked.items() if r.startswith("wrong_person")]
    assert len(wrong) == 15
    wrong_cats = {by_id[t]["category_name"] for t in wrong}
    assert {"Out Of Office", "Do Not Contact"} <= wrong_cats
    assert labels["off_script"] >= 15
    assert reasons["low_confidence"] > 0
    common = {d["message_type"] for d in drafts} - set(mk.RARE_LABELS)
    assert all(labels[label] >= mk.LABEL_FLOOR for label in common)


def test_low_confidence_picks_are_the_lowest_unpicked() -> None:
    drafts = mk.usable_drafts(synthetic_drafts())
    picked = mk.sample_threads(drafts, seed=7, total=120)
    low_ids = {d["thread_id"] for d in drafts if d["confidence"] < 0.2}
    assert low_ids <= set(picked)
    assert all(picked[t] == "low_confidence" for t in low_ids)


def test_sample_is_deterministic_and_seed_dependent() -> None:
    drafts = mk.usable_drafts(synthetic_drafts())
    a = mk.sample_threads(drafts, seed=7, total=120)
    assert a == mk.sample_threads(drafts, seed=7, total=120)
    assert set(a) != set(mk.sample_threads(drafts, seed=8, total=120))


def test_sample_fails_when_not_enough_threads() -> None:
    drafts = mk.usable_drafts(synthetic_drafts()[:30])
    with pytest.raises(SystemExit):
        mk.sample_threads(drafts, seed=7, total=200)


def test_error_rows_are_never_sampled() -> None:
    rows = synthetic_drafts()
    rows[0]["status"] = "error"
    assert all(d["thread_id"] != rows[0]["thread_id"] for d in mk.usable_drafts(rows))


def test_sheet_is_blind_masked_trimmed_and_shuffled() -> None:
    drafts = synthetic_drafts()
    sheet, key = mk.build(dataset_for(drafts), drafts, seed=7, total=120)
    assert list(sheet[0]) == mk.SHEET_FIELDS
    assert not any("draft" in f or f in {"message_type", "lead_verdict"} for f in sheet[0])
    row = sheet[0]
    assert row["human_message_type"] == row["unsure"] == ""
    for r in sheet:
        assert "sam@firm.com" not in r["outreach_snippet"] + r["prospect_first_reply"]
        assert len(r["outreach_snippet"]) <= 300 and len(r["prospect_first_reply"]) <= 800
        assert len(r["gamic_response_to_first_reply"]) <= 400
    assert "[email]" in sheet[0]["outreach_snippet"]
    assert [r["thread_id"] for r in sheet] != [k["thread_id"] for k in key]
    assert {r["thread_id"] for r in sheet} == {k["thread_id"] for k in key}
    assert list(key[0]) == mk.KEY_FIELDS and key[0]["sample_reason"]


def test_trim_masks_before_cutting() -> None:
    assert mk.trim("a" * 5 + " x@y.com", 13) == "aaaaa [email]"
    assert mk.trim("a" * 5 + " x@y.com", 8) == "aaaaa [e"  # an unmasked cut would leak "x@"


def test_main_refuses_to_overwrite_labeled_sheet(tmp_path: Path) -> None:
    drafts = synthetic_drafts()
    data, dr = tmp_path / "d.csv", tmp_path / "dr.csv"
    write_rows(data, dataset_for(drafts))
    write_rows(dr, drafts)
    sheet, key = tmp_path / "sheet.csv", tmp_path / "key.csv"
    args = ["--dataset", str(data), "--drafts", str(dr), "--sheet", str(sheet), "--key", str(key)]
    assert mk.main([*args, "--total", "120"]) == 0
    rows = mk.read_csv(sheet)
    rows[0]["human_message_type"] = "info_request"
    mk.write_csv(sheet, mk.SHEET_FIELDS, rows)
    assert mk.main([*args, "--total", "120"]) == 1
    assert mk.read_csv(sheet)[0]["human_message_type"] == "info_request"
    assert mk.main([*args, "--total", "120", "--force"]) == 0


# ------------------------------------------------------------------ scoring


def scoring_files(tmp_path: Path, humans: list[tuple[str, str, str, str]]) -> list[str]:
    """humans: (thread_id, message_type, verdict, template) as the labeler entered them."""
    key = [
        {"thread_id": "t1", "sample_reason": "x", "message_type": "info_request",
         "lead_verdict": "pass", "playbook_template": "T2", "confidence": "0.9"},
        {"thread_id": "t2", "sample_reason": "x", "message_type": "wrong_person",
         "lead_verdict": "flag", "playbook_template": "none", "confidence": "0.8"},
        {"thread_id": "t3", "sample_reason": "x", "message_type": "off_script",
         "lead_verdict": "flag", "playbook_template": "none", "confidence": "0.6"},
    ]  # fmt: skip
    sheet = []
    for tid, mt, verdict, template in humans:
        row = {k: "" for k in mk.SHEET_FIELDS}
        row.update(
            thread_id=tid,
            human_message_type=mt,
            human_verdict=verdict,
            human_template=template,
            notes=f"secret note {tid}",
        )
        sheet.append(row)
    write_rows(tmp_path / "key.csv", key)
    write_rows(tmp_path / "sheet.csv", sheet)
    return [
        "--sheet", str(tmp_path / "sheet.csv"),
        "--key", str(tmp_path / "key.csv"),
        "--gold", str(tmp_path / "gold.csv"),
        "--labeler", "ray",
    ]  # fmt: skip


def test_scoring_reports_agreement_and_disagreements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = scoring_files(
        tmp_path,
        [
            ("t1", "info_request", "pass", "T2"),  # all agree
            ("t2", "out_of_office", "flag", ""),  # label differs, blank template = none
            ("t3", "off_script", "pass", "none"),  # verdict differs
        ],
    )
    assert sc.main(args) == 0
    out = capsys.readouterr().out
    assert "message_type: 2/3 agree (66.7%)" in out
    assert "lead_verdict: 2/3 agree (66.7%)" in out
    assert "playbook_template: 3/3 agree (100.0%)" in out
    assert "t2" in out and "wrong_person" in out and "out_of_office" in out
    assert "draft wrong_person -> human out_of_office: 1" in out
    assert "secret note" not in out  # notes are never printed


def test_scoring_saves_gold_set(tmp_path: Path) -> None:
    humans = [("t1", "info_request", "pass", "T2"), ("t2", "wrong_person", "flag", "")]
    args = scoring_files(tmp_path, [*humans, ("t3", "", "", "")])
    assert sc.main(args) == 0
    gold = sc.read_csv(tmp_path / "gold.csv")
    assert list(gold[0]) == sc.GOLD_FIELDS
    assert [g["thread_id"] for g in gold] == ["t1", "t2"]  # incomplete row skipped
    assert gold[1]["human_template"] == "none" and gold[1]["labeler"] == "ray"
    assert gold[0]["notes"] == "secret note t1"


def test_scoring_rejects_invalid_values_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    humans = [("t1", "made_up", "pass", "T9"), ("t2", "wrong_person", "ok", "")]
    assert sc.main(scoring_files(tmp_path, humans)) == 1
    out = capsys.readouterr().out
    assert "t1: human_message_type = 'made_up'" in out
    assert "t1: human_template = 'T9'" in out
    assert "t2: human_verdict = 'ok'" in out
    assert not (tmp_path / "gold.csv").exists()


def test_scoring_unsure_rows_are_reported_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    humans = [("t1", "info_request", "pass", "T2"), ("t2", "off_script", "flag", "")]
    args = scoring_files(tmp_path, humans)
    sheet = sc.read_csv(tmp_path / "sheet.csv")
    sheet[1]["unsure"] = "Y"
    write_rows(tmp_path / "sheet.csv", sheet)
    assert sc.main(args) == 0
    out = capsys.readouterr().out
    assert "Marked unsure: 1" in out
    assert "excluding rows marked unsure: 1/1 (100.0%)" in out


def test_scoring_with_nothing_filled_exits_cleanly(tmp_path: Path) -> None:
    args = scoring_files(tmp_path, [("t1", "", "", "")])
    assert sc.main(args) == 1
    assert not (tmp_path / "gold.csv").exists()
