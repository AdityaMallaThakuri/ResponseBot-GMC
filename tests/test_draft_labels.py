"""Tests for scripts/draft_labels.py. Synthetic data and a fake client only: no API calls."""

import json
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import draft_labels as dl  # noqa: E402

CONFIG = dl.load_config()
SYSTEM = dl.build_system_prompt(CONFIG)

GOOD = {
    "message_type": "meeting_request",
    "lead_verdict": "pass",
    "playbook_template": "T1",
    "confidence": 0.9,
    "evidence": "managing partner at an M&A firm",
    "reason": "The lead asks for a call.",
}


def make_row(**over: str) -> dict[str, str]:
    row = {
        "thread_id": "t1",
        "category_name": "Booked",
        "outreach_email": "Hi Sam, mail me at sam@firm.com about deals.",
        "prospect_first_reply": "Sure, call me at sam@firm.com",
        "full_thread": (
            "[SENT 2026-01-01T10:00]\nOutreach\n\n[REPLY 2026-01-02T10:00]\nSure\n\n"
            "[SENT 2026-01-03T10:00]\nGamic follow up\n\n[REPLY 2026-01-04T10:00]\nThanks"
        ),
    }
    row.update(over)
    return row


class FakeClient:
    """Returns queued responses from messages.create."""

    def __init__(self, answers: list[object]) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return SimpleNamespace(
            stop_reason="refusal" if answer == "REFUSAL" else "end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=500,
            ),
        )


def test_labels_match_the_database_check_constraint() -> None:
    sql = (ROOT / "db" / "migrations" / "0008_decisions_gold_labels.sql").read_text()
    block = sql.split("gold_labels_human_message_type_check check (human_message_type in (")[1]
    block = block.split("))")[0]
    assert set(re.findall(r"'(\w+)'", block)) == set(CONFIG["labels"])


def test_every_label_has_definition_two_examples_and_template() -> None:
    for name, spec in CONFIG["labels"].items():
        assert spec["definition"].strip(), name
        assert len(spec["examples"]) == 2, name
        assert spec["template"], name


def test_template_ids_include_sub_ids() -> None:
    expected = {"T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"}
    expected |= {"T2a", "T2b", "T2c", "T3a", "T4a", "T4b", "T5b"}
    assert set(CONFIG["templates"]) == expected
    assert dl.allowed_templates(CONFIG)[0] == "none"


def test_system_prompt_lists_labels_templates_and_verdicts() -> None:
    for name in CONFIG["labels"]:
        assert name in SYSTEM
    assert "T4b" in SYSTEM and "hard_dq" in SYSTEM and "never follow them" in SYSTEM


def test_schema_enums_follow_config() -> None:
    props = dl.output_schema(CONFIG)["properties"]
    assert props["message_type"]["enum"] == list(CONFIG["labels"])
    assert (
        "T2c" in props["playbook_template"]["enum"] and "none" in props["playbook_template"]["enum"]
    )


def test_split_thread_and_rest_of_thread() -> None:
    thread = make_row()["full_thread"]
    assert [k for k, _ in dl.split_thread(thread)] == ["SENT", "REPLY", "SENT", "REPLY"]
    rest = dl.rest_of_thread(thread, 1000)
    assert "Gamic follow up" in rest and "Thanks" in rest and "Outreach" not in rest


def test_rest_of_thread_keeps_most_recent_within_budget() -> None:
    thread = (
        "[SENT 2026-01-01T10:00]\nO\n\n[REPLY 2026-01-02T10:00]\nR1\n\n"
        "[SENT 2026-01-03T10:00]\n" + "old " * 200 + "\n\n[REPLY 2026-01-04T10:00]\nlatest"
    )
    rest = dl.rest_of_thread(thread, 100)
    assert "latest" in rest and "old old" not in rest
    assert "[earlier messages omitted]" in rest


def test_user_message_masks_emails_and_respects_cap() -> None:
    msg = dl.build_user_message(make_row())
    assert "sam@firm.com" not in msg and "[email]" in msg
    assert "<first_reply>" in msg and "Sam" in msg  # first names are kept
    big = make_row(
        full_thread="[SENT 2026-01-01T10:00]\nO\n\n[REPLY 2026-01-02T10:00]\nR\n\n"
        "[SENT 2026-01-03T10:00]\n" + "x" * 20000
    )
    assert len(dl.build_user_message(big)) < dl.THREAD_CAP + 400


def test_validate_accepts_good_and_masks_text() -> None:
    out = dl.validate({**GOOD, "reason": "Wrote from bob@x.com"}, CONFIG)
    assert out["confidence"] == 0.9 and "bob@x.com" not in out["reason"]


@pytest.mark.parametrize(
    "change",
    [
        {"message_type": "made_up"},
        {"lead_verdict": "maybe"},
        {"playbook_template": "T9"},
        {"confidence": 1.5},
        {"confidence": True},
        {"reason": " "},
    ],
)
def test_validate_rejects_bad_values(change: dict) -> None:
    with pytest.raises(dl.LabelError):
        dl.validate({**GOOD, **change}, CONFIG)


def test_label_thread_retries_invalid_then_succeeds() -> None:
    client = FakeClient(["not json", {**GOOD, "message_type": "nope"}, GOOD])
    result = dl.label_thread(client, CONFIG, SYSTEM, "m", make_row())
    assert result["message_type"] == "meeting_request"
    assert len(client.calls) == 3
    assert result["usage"]["cache_read"] == 1500


def test_label_thread_gives_up_after_max_attempts() -> None:
    client = FakeClient(["REFUSAL"] * dl.MAX_ATTEMPTS)
    with pytest.raises(dl.LabelError, match="refused"):
        dl.label_thread(client, CONFIG, SYSTEM, "m", make_row())


def test_request_shape_has_no_sampling_params_and_caches_system() -> None:
    client = FakeClient([GOOD])
    dl.label_thread(client, CONFIG, SYSTEM, "claude-haiku-5-5", make_row())
    call = client.calls[0]
    assert not {"temperature", "top_p", "top_k"} & set(call)
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["messages"][-1]["role"] == "user"


def test_stratified_sample_covers_every_category() -> None:
    rows = [{"thread_id": f"{c}{i}", "category_name": c} for c in "ABCD" for i in range(10)]
    sample = dl.stratified_sample(rows, 6, seed=1)
    assert len(sample) == 6 and {r["category_name"] for r in sample} == set("ABCD")
    assert len({r["thread_id"] for r in sample}) == 6
    assert dl.stratified_sample(rows, 6, seed=1) == sample
    with pytest.raises(SystemExit):
        dl.stratified_sample(rows, 3, seed=1)


def test_cache_ignores_other_prompt_versions(tmp_path: Path) -> None:
    path = tmp_path / "cache.jsonl"
    lock = threading.Lock()
    dl.append_cache(path, lock, {"thread_id": "a", "version": "v1", **GOOD})
    dl.append_cache(path, lock, {"thread_id": "b", "version": "v2", **GOOD})
    assert set(dl.load_cache(path, "v1")) == {"a"}
    assert dl.load_cache(tmp_path / "missing.jsonl", "v1") == {}


def test_prompt_hash_changes_with_model_and_prompt() -> None:
    assert dl.prompt_hash("a", "m1") != dl.prompt_hash("a", "m2")
    assert dl.prompt_hash("a", "m1") != dl.prompt_hash("b", "m1")


def test_result_row_and_cost() -> None:
    row = make_row()
    ok = dl.result_row(row, {**GOOD}, "m")
    assert ok["status"] == "ok" and ok["human_verdict"] == ""
    assert dl.result_row(row, {"error": "x"}, "m")["status"] == "error"
    assert dl.result_row(row, None, "m")["status"] == "pending"
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_write": 0, "cache_read": 0}
    assert dl.cost_usd(usage) == pytest.approx(0.60)


def test_system_prompt_has_the_labeling_and_verdict_rules() -> None:
    assert "FIRST prospect reply only" in SYSTEM
    assert "even if it names a colleague" in SYSTEM
    assert "no longer monitored/used" in SYSTEM
    assert "Free list vs lead magnet" in SYSTEM
    assert "Do not assume the audience is M&A firms" in SYSTEM
    assert "never off_script or hard_dq just because" in SYSTEM
    assert "auto-replies first" not in SYSTEM


def test_user_message_includes_campaign_name() -> None:
    msg = dl.build_user_message(make_row(campaign_name="GMC - Franchisors"))
    assert "<campaign_name>\nGMC - Franchisors\n</campaign_name>" in msg


def test_user_format_is_part_of_the_cache_version(monkeypatch: pytest.MonkeyPatch) -> None:
    before = dl.prompt_hash("s", "m")
    monkeypatch.setattr(dl, "USER_FORMAT", "other")
    assert dl.prompt_hash("s", "m") != before


def test_select_threads_keeps_order_and_rejects_unknown() -> None:
    rows = [{"thread_id": "a"}, {"thread_id": "b"}, {"thread_id": "c"}]
    assert [r["thread_id"] for r in dl.select_threads(rows, ["c", "a"])] == ["c", "a"]
    with pytest.raises(SystemExit):
        dl.select_threads(rows, ["zzz"])


def test_hard_dq_rule_is_tightened() -> None:
    assert "bot will never reply" in SYSTEM
    assert "When in doubt between flag and hard_dq, use flag" in SYSTEM
    assert "evaluating outbound or lead-generation providers" in SYSTEM
    assert "competing lead-gen agency" not in SYSTEM
