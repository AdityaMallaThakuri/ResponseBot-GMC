"""Tests for the confidence threshold and its setting."""

from pathlib import Path

import pytest

from app.classify.classifier import Classification, apply_rules
from app.classify.confidence import CONTINUE, HUMAN_ONLY, lane_after_threshold, needs_human
from app.config.settings import DEFAULT_MIN_CONFIDENCE, REQUIRED, ConfigError, get_settings
from tests.conftest import REQUIRED as GOOD_VALUES


def llm(confidence: float, label: str = "info_request") -> Classification:
    return Classification(label, confidence, "reason", "llm")


def test_below_the_threshold_goes_to_a_human() -> None:
    assert lane_after_threshold(llm(0.74), 0.75) == HUMAN_ONLY
    assert lane_after_threshold(llm(0.0), 0.75) == HUMAN_ONLY
    assert needs_human(llm(0.5), 0.75)


def test_at_or_above_the_threshold_continues() -> None:
    assert lane_after_threshold(llm(0.75), 0.75) == CONTINUE
    assert lane_after_threshold(llm(0.99), 0.75) == CONTINUE
    assert not needs_human(llm(0.8), 0.75)


def test_rule_results_always_pass() -> None:
    result = apply_rules("Please remove me from your list")
    assert result is not None and result.confidence == 1.0
    assert lane_after_threshold(result, 0.75) == CONTINUE
    assert lane_after_threshold(result, 1.0) == CONTINUE


def test_a_low_confidence_stop_request_is_not_auto_handled() -> None:
    assert lane_after_threshold(llm(0.6, "do_not_contact"), 0.75) == HUMAN_ONLY


def test_no_classification_goes_to_a_human() -> None:
    assert lane_after_threshold(None, 0.75) == HUMAN_ONLY


def test_threshold_is_adjustable() -> None:
    assert lane_after_threshold(llm(0.7), 0.6) == CONTINUE
    assert lane_after_threshold(llm(0.7), 0.9) == HUMAN_ONLY


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for name in REQUIRED:
        monkeypatch.setenv(name, GOOD_VALUES[name])
    monkeypatch.delenv("CLASSIFIER_MIN_CONFIDENCE", raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_setting_defaults_to_0_75(env: pytest.MonkeyPatch) -> None:
    assert DEFAULT_MIN_CONFIDENCE == 0.75
    assert get_settings().CLASSIFIER_MIN_CONFIDENCE == 0.75


def test_setting_can_be_overridden(env: pytest.MonkeyPatch) -> None:
    env.setenv("CLASSIFIER_MIN_CONFIDENCE", "0.9")
    assert get_settings().CLASSIFIER_MIN_CONFIDENCE == 0.9


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_setting_falls_back_to_the_default(env: pytest.MonkeyPatch, blank: str) -> None:
    env.setenv("CLASSIFIER_MIN_CONFIDENCE", blank)
    assert get_settings().CLASSIFIER_MIN_CONFIDENCE == 0.75


@pytest.mark.parametrize("bad", ["1.5", "-0.1", "high"])
def test_invalid_setting_is_rejected(env: pytest.MonkeyPatch, bad: str) -> None:
    env.setenv("CLASSIFIER_MIN_CONFIDENCE", bad)
    with pytest.raises(ConfigError, match="CLASSIFIER_MIN_CONFIDENCE"):
        get_settings()


def test_model_settings_default_to_haiku_and_blank_is_default(env: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert settings.CLASSIFIER_MODEL == settings.DRAFT_LABEL_MODEL == "claude-haiku-5-5"
    get_settings.cache_clear()
    env.setenv("CLASSIFIER_MODEL", "")
    assert get_settings().CLASSIFIER_MODEL == "claude-haiku-5-5"
