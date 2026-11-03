from functools import lru_cache

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SMARTLEAD_API_KEY: str
    ANTHROPIC_API_KEY: str
    SLACK_BOT_TOKEN: str
    SLACK_SIGNING_SECRET: str
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    SEND_ENABLED: bool = False
    WEBHOOK_SECRET: str


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        name = ".".join(str(part) for part in item["loc"])
        problem = "missing" if item["type"] == "missing" else item["msg"]
        lines.append(f"  - {name}: {problem}")
    return "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(
            "Invalid configuration. Set these variables in .env or the environment "
            "(see .env.example):\n" + _describe(exc)
        ) from None
