import pytest
from fastapi.testclient import TestClient

from app.config.settings import ConfigError, get_settings
from app.main import app
from tests.conftest import REQUIRED


def test_health(env: None) -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_send_enabled_defaults_false(env: None) -> None:
    assert get_settings().SEND_ENABLED is False


def test_missing_variables_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir("tests")  # no .env here
    get_settings.cache_clear()
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert "SMARTLEAD_API_KEY: missing" in str(exc.value)
    assert "WEBHOOK_SECRET: missing" in str(exc.value)
    get_settings.cache_clear()
