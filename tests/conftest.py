import pytest

from app.config.settings import get_settings

REQUIRED = {
    "SMARTLEAD_API_KEY": "test",
    "ANTHROPIC_API_KEY": "test",
    "SLACK_BOT_TOKEN": "test",
    "SLACK_SIGNING_SECRET": "test",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "test",
    "WEBHOOK_SECRET": "w" * 32 + "-test-webhook-secret",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
