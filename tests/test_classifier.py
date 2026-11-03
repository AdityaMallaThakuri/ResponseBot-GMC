"""Tests for app/classify/classifier.py. Synthetic text and a fake client only: no API calls."""

import json
from types import SimpleNamespace

import pytest

from app.classify import classifier as cl

CONFIG = cl.load_config()


class FakeClient:
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
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=100,
            ),
        )


GOOD = {"label": "info_request", "confidence": 0.8, "reason": "Asks how it works."}


def make(answers: list[object]) -> tuple[cl.Classifier, FakeClient]:
    client = FakeClient(answers)
    return cl.Classifier(client, "claude-haiku-5-5", CONFIG), client


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Please unsubscribe me", "do_not_contact"),
        ("Remove me from your list.", "do_not_contact"),
        ("take us off your list", "do_not_contact"),
        ("STOP", "do_not_contact"),
        ("Please stop emailing me", "do_not_contact"),
        ("Do not contact me again.", "do_not_contact"),
        ("don't email me", "do_not_contact"),
        (
            "Please verify your email to deliver your message. Powered by Sendio",
            "auto_reply_or_bounce",
        ),
        ("Sendio: sender verification required", "auto_reply_or_bounce"),
        ("Jane is no longer with Acme. Contact Bob.", "wrong_person"),
        ("He has left the company.", "wrong_person"),
        ("I no longer work for the firm", "wrong_person"),
    ],
)
def test_rules_fire_on_the_obvious_cases(text: str, label: str) -> None:
    result = cl.apply_rules(text)
    assert result is not None and result.label == label
    assert result.confidence == 1.0 and result.source == "rule"


@pytest.mark.parametrize(
    "text",
    [
        "Sure, send the list over.",
        "I am out of the office until Monday.",
        "How much does it cost?",
        "We are no longer interested.",  # the model decides this one
        "Not interested, thanks.",
        "",
    ],
)
def test_rules_stay_quiet_otherwise(text: str) -> None:
    assert cl.apply_rules(text) is None


def test_rule_hit_makes_no_api_call() -> None:
    clf, client = make([])
    result = clf.classify("Please remove me from your list", "GMC - Test", "outreach")
    assert result.label == "do_not_contact" and result.source == "rule"
    assert client.calls == []


def test_quoted_history_is_cleaned_before_the_rules() -> None:
    clf, client = make([GOOD])
    reply = "Sure, tell me more.\n\nOn Mon, Jan 1, 2026, Ann wrote:\n> To unsubscribe reply STOP"
    result = clf.classify(reply, "GMC - Test", "outreach")
    assert result.source == "llm" and len(client.calls) == 1


def test_model_path_maps_the_answer_and_usage() -> None:
    clf, _ = make([GOOD])
    result = clf.classify("How does it work?", "GMC - Test", "We offer X")
    assert (result.label, result.confidence, result.source) == ("info_request", 0.8, "llm")
    assert result.usage and result.usage["cache_read"] == 100


def test_request_has_no_sampling_params_and_uses_cache_and_schema() -> None:
    clf, client = make([GOOD])
    clf.classify("How does it work?", "GMC - Test", "We offer X")
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert not {"temperature", "top_p", "top_k"} & set(call)
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["output_config"]["format"]["type"] == "json_schema"
    enum = call["output_config"]["format"]["schema"]["properties"]["label"]["enum"]
    assert enum == list(CONFIG["labels"])


def test_invalid_answers_are_retried_then_raise() -> None:
    clf, client = make(["nope", {**GOOD, "label": "bogus"}, {**GOOD, "confidence": 2}])
    with pytest.raises(cl.ClassifierError, match="invalid answer"):
        clf.classify("hi", "c", "o")
    assert len(client.calls) == cl.MAX_ATTEMPTS
    clf, _ = make(["nope", GOOD])
    assert clf.classify("hi", "c", "o").label == "info_request"


def test_refusal_raises_after_retries() -> None:
    clf, _ = make(["REFUSAL"] * cl.MAX_ATTEMPTS)
    with pytest.raises(cl.ClassifierError, match="refused"):
        clf.classify("hi", "c", "o")


def test_system_prompt_has_labels_and_rules_but_no_verdict_or_template() -> None:
    prompt = cl.build_system_prompt(CONFIG)
    for name in CONFIG["labels"]:
        assert name in prompt
    assert "FIRST prospect reply only" in prompt and "Free list vs lead magnet" in prompt
    assert "lead_verdict" not in prompt and "hard_dq" not in prompt
    assert "playbook_template" not in prompt


def test_user_message_masks_emails_and_caps_the_reply() -> None:
    msg = cl.build_user_message("Mail me at bob@x.com " + "r" * 5000, "GMC - Test", "Hi, ann@y.org")
    assert "bob@x.com" not in msg and "ann@y.org" not in msg and msg.count("[email]") == 2
    assert "<campaign_name>\nGMC - Test" in msg
    assert len(msg) < cl.REPLY_CAP + 300
    assert "(empty)" in cl.build_user_message("", "c", "o")


def test_prompt_version_changes_with_model_and_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    system = cl.build_system_prompt(CONFIG)
    base = cl.prompt_version("m1", system)
    assert base != cl.prompt_version("m2", system)
    monkeypatch.setattr(cl, "RULES", cl.RULES[:-1])
    assert base != cl.prompt_version("m1", system)
