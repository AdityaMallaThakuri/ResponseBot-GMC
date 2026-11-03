from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class SuppressionScope(StrEnum):
    CAMPAIGN = "campaign"
    CLIENT = "client"
    GLOBAL = "global"


@dataclass(frozen=True)
class Suppression:
    email: str
    scope: SuppressionScope
    reason: str
    campaign_id: int | None = None
    client: str | None = None
    source_reply_id: str | None = None
    confirmed_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def build_suppression(
    email: str,
    scope: SuppressionScope,
    reason: str,
    *,
    campaign_id: int | None = None,
    client: str | None = None,
    source_reply_id: str | None = None,
    confirmed_by: str | None = None,
) -> Suppression:
    """Build a suppression record, enforcing the same rules as the table constraints."""
    if scope is SuppressionScope.CAMPAIGN and campaign_id is None:
        raise ValueError("campaign-scope suppression requires campaign_id")
    if scope is SuppressionScope.CLIENT and not client:
        raise ValueError("client-scope suppression requires client")
    if scope is SuppressionScope.GLOBAL and not confirmed_by:
        raise ValueError("global suppression requires confirmed_by (human confirmation in Slack)")
    return Suppression(
        email=email.strip().lower(),
        scope=scope,
        reason=reason,
        campaign_id=campaign_id,
        client=client,
        source_reply_id=source_reply_id,
        confirmed_by=confirmed_by,
    )
