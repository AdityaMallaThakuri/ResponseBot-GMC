"""Tests for the safety rules in app/classify/policy.py and the not_interested threshold."""

from pathlib import Path

import pytest

from app.classify.classifier import Classification, apply_rules, load_config
from app.classify.confidence import CONTINUE, HUMAN_ONLY
from app.classify.policy import (
    AUTO_ACTION_LABELS,
    AUTO_SUPPRESS,
    NEVER_AUTO_CLOSE,
    SUGGEST_CLOSE,
    AutoActionNotAllowed,
    decide,
    may_auto_suppress,
    require_auto_action_allowed,
)
from app.config.settings import REQUIRED, ConfigError, get_settings
from tests.conftest import REQUIRED as GOOD_VALUES

ALL_LABELS = list(load_config()["labels"])
MIN, MIN_NI = 0.75, 0.90


def llm(label: str, confidence: float) -> Classification:
    return Classification(label, confidence, "reason", "llm")


def test_only_do_not_contact_may_trigger_an_automatic_action() -> None:
    assert {"do_not_contact"} == AUTO_ACTION_LABELS
    assert may_auto_suppress("do_not_contact")
    for label in ALL_LABELS:
        if label != "do_not_contact":
            assert not may_auto_suppress(label)
            with pytest.raises(AutoActionNotAllowed):
                require_auto_action_allowed(label)
    require_auto_action_allowed("do_not_contact")  # does not raise


@pytest.mark.parametrize("label", [label for label in ALL_LABELS if label != "do_not_contact"])
@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.75, 0.9, 1.0])
def test_no_other_label_ever_gets_an_automatic_action(label: str, confidence: float) -> None:
    assert decide(llm(label, confidence), MIN, MIN_NI).auto_action is None


def test_do_not_contact_suppresses_automatically_when_confident() -> None:
    rule = apply_rules("Please remove me from your list")
    assert rule is not None
    d = decide(rule, MIN, MIN_NI)
    assert (d.lane, d.auto_action) == (CONTINUE, AUTO_SUPPRESS)
    assert decide(llm("do_not_contact", 0.75), MIN, MIN_NI).auto_action == AUTO_SUPPRESS


def test_low_confidence_do_not_contact_goes_to_a_human_instead() -> None:
    d = decide(llm("do_not_contact", 0.74), MIN, MIN_NI)
    assert (d.lane, d.auto_action) == (HUMAN_ONLY, None)


def test_not_interested_below_the_threshold_is_human_only_with_no_note() -> None:
    for confidence in (0.0, 0.7, 0.8, 0.89):
        d = decide(llm("not_interested", confidence), MIN, MIN_NI)
        assert (d.lane, d.auto_action, d.note) == (HUMAN_ONLY, None, None)


def test_not_interested_at_or_above_the_threshold_suggests_close_but_does_not_act() -> None:
    for confidence in (0.90, 0.95, 1.0):
        d = decide(llm("not_interested", confidence), MIN, MIN_NI)
        assert (d.lane, d.auto_action, d.note) == (HUMAN_ONLY, None, SUGGEST_CLOSE)


def test_not_interested_rule_result_still_needs_a_human() -> None:
    d = decide(Classification("not_interested", 1.0, "rule", "rule"), MIN, MIN_NI)
    assert (d.lane, d.auto_action, d.note) == (HUMAN_ONLY, None, SUGGEST_CLOSE)


def test_objections_are_never_closed_or_suppressed() -> None:
    assert {"not_interested", "objection_info", "objection_price"} == NEVER_AUTO_CLOSE
    for label in ("objection_info", "objection_price"):
        d = decide(llm(label, 0.95), MIN, MIN_NI)
        assert (d.lane, d.auto_action, d.note) == (CONTINUE, None, None)  # a draft for approval
        assert decide(llm(label, 0.6), MIN, MIN_NI).lane == HUMAN_ONLY


def test_missing_classification_goes_to_a_human() -> None:
    d = decide(None, MIN, MIN_NI)
    assert (d.lane, d.auto_action) == (HUMAN_ONLY, None)


def test_general_threshold_applies_before_the_not_interested_one() -> None:
    d = decide(llm("not_interested", 0.5), 0.75, 0.4)  # NI threshold lower than the general one
    assert (d.lane, d.note) == (HUMAN_ONLY, None)


# ------------------------------------------------------------------ the new setting


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for name in REQUIRED:
        monkeypatch.setenv(name, GOOD_VALUES[name])
    for name in ("CLASSIFIER_MIN_CONFIDENCE", "CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_not_interested_setting_defaults_to_0_90(env: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert settings.CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED == 0.90
    assert settings.CLASSIFIER_MIN_CONFIDENCE == 0.75  # the other default is unchanged


def test_not_interested_setting_override_and_blank(env: pytest.MonkeyPatch) -> None:
    env.setenv("CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED", "0.95")
    assert get_settings().CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED == 0.95
    get_settings.cache_clear()
    env.setenv("CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED", "  ")
    assert get_settings().CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED == 0.90


@pytest.mark.parametrize("bad", ["1.2", "-1", "x"])
def test_not_interested_setting_rejects_bad_values(env: pytest.MonkeyPatch, bad: str) -> None:
    env.setenv("CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED", bad)
    with pytest.raises(ConfigError, match="CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED"):
        get_settings()
