"""The things ingest needs, bundled so tests can swap in fakes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config.settings import Settings
from app.db.client import Database


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IngestConfig:
    """Routing thresholds and Slack channels (see app/classify/routing.py)."""

    min_confidence: float = 0.75
    min_confidence_not_interested: float = 0.90
    draft_channel: str | None = None  # draft-lane cards
    alert_channel: str | None = None  # human_only and would_suppress notices; falls back to draft

    def channel_for(self, lane: str) -> str | None:
        if lane == "draft":
            return self.draft_channel
        return self.alert_channel or self.draft_channel

    @classmethod
    def from_settings(cls, settings: Settings) -> IngestConfig:
        return cls(
            min_confidence=settings.CLASSIFIER_MIN_CONFIDENCE,
            min_confidence_not_interested=settings.CLASSIFIER_MIN_CONFIDENCE_NOT_INTERESTED,
            draft_channel=settings.SLACK_DRAFT_CHANNEL,
            alert_channel=settings.SLACK_ALERT_CHANNEL,
        )


@dataclass
class IngestDeps:
    db: Database
    smartlead: Any  # SmartleadClient, or a fake with get_thread_raw and find_inbox_item
    clock: Callable[[], datetime] = field(default=utc_now)
    # C2. Without a classifier and a Slack client, ingest only stores threads (C1 behaviour).
    classifier: Any | None = None  # app.classify.classifier.Classifier or a fake
    slack: Any | None = None  # slack_sdk.WebClient or a fake with chat_postMessage
    config: IngestConfig = field(default_factory=IngestConfig)


def build_deps(settings: Settings) -> IngestDeps:
    """The real services. Ingest only ever reads from Smartlead; SEND_ENABLED is not used here."""
    import anthropic
    from slack_sdk import WebClient

    from app.classify.classifier import Classifier
    from app.db.connect import create_db
    from app.smartlead.client import SmartleadClient

    claude = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, max_retries=5)
    return IngestDeps(
        db=create_db(settings),
        smartlead=SmartleadClient.from_settings(settings),
        classifier=Classifier(claude, settings.CLASSIFIER_MODEL),
        slack=WebClient(token=settings.SLACK_BOT_TOKEN),
        config=IngestConfig.from_settings(settings),
    )
