from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.db.suppressions import SuppressionScope


class Direction(StrEnum):
    SENT = "SENT"
    REPLY = "REPLY"


@dataclass(frozen=True)
class Message:
    direction: Direction
    timestamp: datetime
    text: str
    stats_id: str


@dataclass(frozen=True)
class Lead:
    campaign_id: int
    lead_id: int
    email: str | None = None


@dataclass(frozen=True)
class Thread:
    lead: Lead
    messages: list[Message] = field(default_factory=list)
    # stats_id of the latest lead reply, the message send_reply answers.
    reply_to_stats_id: str | None = None
    to_email: str | None = None


@dataclass(frozen=True)
class UnsubscribeResult:
    scope: SuppressionScope
    campaign_id: int
    lead_id: int
    ok: bool
