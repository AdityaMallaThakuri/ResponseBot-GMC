from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config.settings import REQUIRED, ConfigError, get_settings
from app.main import app
from tests.conftest import REQUIRED as GOOD_VALUES

OPTIONAL_CHANNELS = [
    "SLACK_DRAFT_CHANNEL",
    "SLACK_LOG_CHANNEL",
    "SLACK_ALERT_CHANNEL",
    "SLACK_SHADOW_CHANNEL",
    "SLACK_OPS_CHANNEL",
]
BLANKS = ["", "   ", "\t"]


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """A clean slate: no .env in the working directory and no inherited settings."""
    monkeypatch.chdir(tmp_path)
    for name in [*REQUIRED, *OPTIONAL_CHANNELS, "SEND_ENABLED", "CATEGORY_WRITEBACK_ENABLED"]:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def set_good_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**GOOD_VALUES, **overrides}.items():
        monkeypatch.setenv(name, value)


def problem_lines(error: ConfigError) -> list[str]:
    return [line.strip() for line in str(error).splitlines() if line.strip().startswith("- ")]


def test_required_list_matches_the_good_env_fixture() -> None:
    assert set(REQUIRED) == set(GOOD_VALUES)


def test_all_required_values_present_is_fine(isolated: pytest.MonkeyPatch) -> None:
    set_good_env(isolated)
    assert get_settings().SLACK_SIGNING_SECRET == "test"


@pytest.mark.parametrize("blank", BLANKS)
@pytest.mark.parametrize("name", REQUIRED)
def test_blank_required_variable_in_the_environment_is_rejected(
    isolated: pytest.MonkeyPatch, name: str, blank: str
) -> None:
    set_good_env(isolated, **{name: blank})
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert problem_lines(exc.value) == [f"- {name}: blank"]


@pytest.mark.parametrize("name", REQUIRED)
def test_absent_required_variable_is_still_reported_as_missing(
    isolated: pytest.MonkeyPatch, name: str
) -> None:
    set_good_env(isolated)
    isolated.delenv(name)
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert problem_lines(exc.value) == [f"- {name}: missing"]


@pytest.mark.parametrize("blank", ["", '"   "'])
@pytest.mark.parametrize("name", REQUIRED)
def test_blank_required_variable_in_dotenv_is_rejected(
    isolated: pytest.MonkeyPatch, tmp_path: Path, name: str, blank: str
) -> None:
    set_good_env(isolated)
    isolated.delenv(name)
    (tmp_path / ".env").write_text(f"{name}={blank}\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert problem_lines(exc.value) == [f"- {name}: blank"]


def test_every_problem_is_listed_in_one_error(isolated: pytest.MonkeyPatch) -> None:
    set_good_env(isolated, SLACK_BOT_TOKEN="", SMARTLEAD_API_KEY="  ")
    isolated.delenv("WEBHOOK_SECRET")
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert sorted(problem_lines(exc.value)) == [
        "- SLACK_BOT_TOKEN: blank",
        "- SMARTLEAD_API_KEY: blank",
        "- WEBHOOK_SECRET: missing",
    ]


def test_error_never_echoes_other_variables_values(isolated: pytest.MonkeyPatch) -> None:
    set_good_env(isolated, SLACK_BOT_TOKEN="xoxb-must-not-leak", SLACK_SIGNING_SECRET="")
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert "xoxb-must-not-leak" not in str(exc.value)


def test_optional_channels_may_still_be_blank(isolated: pytest.MonkeyPatch) -> None:
    set_good_env(isolated)
    for name in OPTIONAL_CHANNELS:
        isolated.setenv(name, "  ")
    settings = get_settings()
    assert all(getattr(settings, name) is None for name in OPTIONAL_CHANNELS)


def test_the_app_refuses_to_start_with_a_blank_signing_secret(
    isolated: pytest.MonkeyPatch,
) -> None:
    set_good_env(isolated, SLACK_SIGNING_SECRET="")
    with pytest.raises(ConfigError, match="SLACK_SIGNING_SECRET: blank"), TestClient(app):
        pass
