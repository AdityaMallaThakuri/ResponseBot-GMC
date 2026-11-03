from functools import lru_cache

from pydantic import ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict

# Variables the app cannot run without. A blank value counts as missing.
REQUIRED = (
    "SMARTLEAD_API_KEY",
    "ANTHROPIC_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "WEBHOOK_SECRET",
)


DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_MIN_CONFIDENCE = 0.75
DEFAULT_MIN_CONFIDENCE_NOT_INTERESTED = 0.90
DEFAULT_DRAFT_MIN_CONFIDENCE = 0.75


MIN_WEBHOOK_SECRET_LENGTH = 32


def parse_campaign_ids(value: str) -> frozenset[int]:
    """ "101, 102" -> {101, 102}. Blank means none. Raises ValueError on anything else."""
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not all(part.isdigit() for part in ids):
        raise ValueError("must be comma-separated campaign ids, for example 101,102")
    return frozenset(int(part) for part in ids)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing, blank or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SMARTLEAD_API_KEY: str
    ANTHROPIC_API_KEY: str
    SLACK_BOT_TOKEN: str
    SLACK_SIGNING_SECRET: str
    SUPABASE_URL: str
    SUPABASE_SERVICE_KEY: str
    SEND_ENABLED: bool = False
    CATEGORY_WRITEBACK_ENABLED: bool = False
    WEBHOOK_SECRET: str
    # Smartlead campaign ids the bot handles, comma separated (for example "101,102").
    # Replies from any other campaign are ignored. Empty means every reply is ignored.
    GMC_CAMPAIGN_IDS: str = ""
    # Model ids, checked against the Anthropic models docs on 2026-11-03.
    CLASSIFIER_MODEL: str = DEFAULT_MODEL
    DRAFT_LABEL_MODEL: str = DEFAULT_MODEL
    # Below this model confidence the thread goes to a human with no draft. Rules score 1.0.
    CLASSIFIER_MIN_CONFIDENCE: float = DEFAULT_MIN_CONFIDENCE
    # not_interested never closes automatically. Below this it goes to human_only; at or above
    # it goes to a human with a "suggest: close" note. Either way no automatic action.
    CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED: float = DEFAULT_MIN_CONFIDENCE_NOT_INTERESTED
    # Drafting (C3). Below this confidence a reply is not drafted: it goes to human_only.
    DRAFT_MIN_CONFIDENCE: float = DEFAULT_DRAFT_MIN_CONFIDENCE
    # The model that fills the few variable slots of a template (never writes free text).
    DRAFT_MODEL: str = "claude-sonnet-5-5"
    # Slack channels (name or id). Optional until the step that posts to them is built.
    SLACK_DRAFT_CHANNEL: str | None = None
    SLACK_LOG_CHANNEL: str | None = None
    SLACK_ALERT_CHANNEL: str | None = None
    SLACK_SHADOW_CHANNEL: str | None = None
    SLACK_OPS_CHANNEL: str | None = None

    @field_validator("CLASSIFIER_MODEL", "DRAFT_LABEL_MODEL", "DRAFT_MODEL", mode="before")
    @classmethod
    def _blank_model_is_default(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @field_validator(
        "CLASSIFIER_MIN_CONFIDENCE",
        "CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED",
        "DRAFT_MIN_CONFIDENCE",
        mode="before",
    )
    @classmethod
    def _blank_confidence_is_default(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @field_validator(
        "CLASSIFIER_MIN_CONFIDENCE",
        "CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED",
        "DRAFT_MIN_CONFIDENCE",
    )
    @classmethod
    def _confidence_in_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between 0 and 1")
        return value

    @field_validator("WEBHOOK_SECRET")
    @classmethod
    def _webhook_secret_is_long_enough(cls, value: str) -> str:
        if value.strip() and len(value.strip()) < MIN_WEBHOOK_SECRET_LENGTH:  # blank: see below
            raise PydanticCustomError(
                "too_short", f"needs at least {MIN_WEBHOOK_SECRET_LENGTH} characters"
            )
        return value

    @field_validator("GMC_CAMPAIGN_IDS")
    @classmethod
    def _campaign_ids_are_integers(cls, value: str) -> str:
        parse_campaign_ids(value)
        return value

    @property
    def gmc_campaign_ids(self) -> frozenset[int]:
        return parse_campaign_ids(self.GMC_CAMPAIGN_IDS)

    @field_validator(*REQUIRED)
    @classmethod
    def _required_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("blank", "blank")
        return value

    @field_validator(
        "SLACK_DRAFT_CHANNEL",
        "SLACK_LOG_CHANNEL",
        "SLACK_ALERT_CHANNEL",
        "SLACK_SHADOW_CHANNEL",
        "SLACK_OPS_CHANNEL",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        name = ".".join(str(part) for part in item["loc"])
        problem = item["msg"] if item["type"] not in ("missing", "blank") else item["type"]
        lines.append(f"  - {name}: {problem}")
    return "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(
            "Invalid configuration. Give a value to these variables in .env or the "
            "environment (see .env.example):\n" + _describe(exc)
        ) from None
