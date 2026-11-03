"""Typed records for the Supabase tables. Field names are the column names.

Enum values mirror the CHECK constraints in db/migrations; a test keeps them in sync.
"""

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from app.smartlead.models import Direction
from app.smartlead.models import Message as ParsedMessage

Row = dict[str, Any]


class RawSource(StrEnum):
    SMARTLEAD_WEBHOOK = "smartlead_webhook"
    SMARTLEAD_POLL = "smartlead_poll"
    SLACK = "slack"
    MANUAL = "manual"


class ThreadStatus(StrEnum):
    OPEN = "open"
    DRAFTED = "drafted"
    AWAITING_HUMAN = "awaiting_human"
    AUTO_HANDLED = "auto_handled"
    SUPPRESSED = "suppressed"
    CLOSED = "closed"


class Verdict(StrEnum):
    PASS = "pass"
    FLAG = "flag"
    HARD_DQ = "hard_dq"


class DraftStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    EDITED = "edited"
    SKIPPED = "skipped"
    SENT = "sent"
    FAILED = "failed"


class CardLane(StrEnum):
    DRAFT = "draft"
    AUTO_ACTION = "auto_action"
    HUMAN_ONLY = "human_only"


class CardStatus(StrEnum):
    PENDING = "pending"
    POSTED = "posted"
    APPROVED = "approved"
    EDITED = "edited"
    SKIPPED = "skipped"
    RESOLVED = "resolved"
    FAILED = "failed"


class ActionType(StrEnum):
    SUPPRESS = "suppress"
    UNSUBSCRIBE = "unsubscribe"
    PAUSE = "pause"
    IGNORE = "ignore"
    SEND = "send"
    UNDO = "undo"
    # recorded instead of SUPPRESS while sending is off: nothing reached Smartlead
    WOULD_SUPPRESS = "would_suppress"


class Decision(StrEnum):
    APPROVED = "approved"
    EDITED = "edited"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    WRONG_LABEL = "wrong_label"
    BAD_DRAFT = "bad_draft"
    NOT_WORTH_REPLYING = "not_worth_replying"
    OTHER = "other"


class RetryKind(StrEnum):
    PROCESS_EVENT = "process_event"
    CLASSIFY = "classify"
    SLACK_POST = "slack_post"
    SMARTLEAD_ACTION = "smartlead_action"
    SEND_REPLY = "send_reply"


class RetryStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    DEAD = "dead"


def payload_hash(payload: dict[str, Any]) -> str:
    """sha256 of the canonical JSON of a payload; key order does not matter."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewRawEvent:
    source: RawSource
    payload: dict[str, Any]
    payload_hash: str

    @classmethod
    def from_payload(cls, source: RawSource, payload: dict[str, Any]) -> "NewRawEvent":
        return cls(source=source, payload=payload, payload_hash=payload_hash(payload))


@dataclass(frozen=True)
class NewThread:
    email_lead_id: int
    email_campaign_id: int
    lead_email: str
    email_lead_map_id: int | None = None
    campaign_name: str | None = None
    lead_first_name: str | None = None
    lead_last_name: str | None = None
    company: str | None = None
    smartlead_category_id: int | None = None
    smartlead_lead_status: str | None = None
    vertical: str | None = None
    last_reply_at: datetime | None = None


@dataclass(frozen=True)
class NewMessage:
    thread_id: str
    direction: Direction
    sent_at: datetime
    stats_id: str
    message_id: str
    from_address: str
    to_address: str
    body_clean: str
    body_html: str | None = None

    @classmethod
    def from_message(cls, thread_id: str, message: ParsedMessage) -> "NewMessage":
        return cls(
            thread_id=thread_id,
            direction=message.direction,
            sent_at=message.timestamp,
            stats_id=message.stats_id,
            message_id=message.message_id,
            from_address=message.from_addr,
            to_address=message.to_addr,
            body_clean=message.text,
        )


@dataclass(frozen=True)
class NewClassification:
    thread_id: str
    reply_message_id: str
    label: str
    confidence: float
    reason: str
    model: str
    prompt_version: str
    smartlead_category_id: int | None = None
    agrees_with_smartlead: bool | None = None
    rule_hit: str | None = None
    # e.g. an out-of-office return date or a referred contact
    extracted_facts: dict[str, Any] | None = None


@dataclass(frozen=True)
class NewQualification:
    thread_id: str
    verdict: Verdict
    icp_version: str
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NewDraft:
    thread_id: str
    classification_id: str
    template_id: str
    template_version: str  # which version of playbook.yaml produced the draft
    text: str
    validator_ok: bool
    slots: dict[str, Any] = field(default_factory=dict)
    validator_reasons: list[str] = field(default_factory=list)
    human_edited: bool = False
    final_text: str | None = None
    status: DraftStatus = DraftStatus.PROPOSED


@dataclass(frozen=True)
class NewDecision:
    draft_id: str
    decided_by: str  # Slack user id
    decision: Decision
    final_text: str | None = None
    skip_reason: SkipReason | None = None
    edit_distance_pct: float | None = None
    decided_at: datetime | None = None
    sent_message_id: str | None = None


@dataclass(frozen=True)
class NewGoldLabel:
    thread_id: str
    human_message_type: str
    human_verdict: Verdict
    labeler: str
    human_template: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class NewSlackCard:
    thread_id: str
    lane: CardLane
    channel: str
    draft_id: str | None = None
    slack_ts: str | None = None
    status: CardStatus = CardStatus.PENDING


@dataclass(frozen=True)
class NewAction:
    thread_id: str
    type: ActionType
    triggered_by: str
    reversible: bool
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NewRetry:
    kind: RetryKind
    payload: dict[str, Any]
    last_error: str | None = None
    next_attempt_at: datetime | None = None


@dataclass(frozen=True)
class NewEvalResult:
    run_id: str
    scorecard: dict[str, Any]
    git_sha: str


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime; use a timezone-aware value")
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def to_row(record: Any) -> Row:
    """Dataclass -> JSON-safe row. None fields are dropped so database defaults apply."""
    row = {f.name: getattr(record, f.name) for f in fields(record)}
    return {key: _jsonable(value) for key, value in row.items() if value is not None}
