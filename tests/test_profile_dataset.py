"""Tests for scripts/profile_dataset.py using synthetic strings only."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import profile_dataset as pd  # noqa: E402


def test_mask_emails_hides_address() -> None:
    masked = pd.mask_emails("write to jane.doe@example.com now")
    assert "jane.doe" not in masked
    assert "example" not in masked
    assert "j***@e***.com" in masked


def test_example_truncates_and_masks() -> None:
    text = "Hi\nplease email bob@acme.org " + "x" * 100
    out = pd.example(text)
    assert len(out) <= 40
    assert "bob" not in out
    assert "\n" not in out


def test_residue_hits() -> None:
    assert pd.residue_hits("On Mon, Ann wrote:\n> hi") == ["wrote:"]
    assert "Original Message" in pd.residue_hits("-----Original Message-----")
    assert "Sent from my" in pd.residue_hits("ok\nSent from my iPhone")
    assert "-- signature" in pd.residue_hits("thanks\n-- \nAnn")
    assert pd.residue_hits("Sounds good, let's talk") == []


def test_first_word() -> None:
    assert pd.first_word("  Thanks, Ann!") == "thanks"
    assert pd.first_word("...") == ""


def test_is_yes() -> None:
    assert pd.is_yes("yes") and pd.is_yes(" TRUE ")
    assert not pd.is_yes("no") and not pd.is_yes("")


def test_build_report_counts() -> None:
    cols = {
        "lead_email": "a@b.com",
        "category_name": "Booked",
        "sentiment": "positive",
        "gamic_replied_after_first_reply": "yes",
        "prospect_first_reply": "Thanks, sounds good",
    }
    rows = [cols, {**cols, "prospect_first_reply": ""}]
    report = pd.build_report(rows)
    assert "Rows: 2" in report
    assert "Unique leads (lead_email): 1" in report
    assert "- 1 rows" in report
    assert "| Booked | 2 | 2 | 100.0% |" in report
    assert "a@b.com" not in report
