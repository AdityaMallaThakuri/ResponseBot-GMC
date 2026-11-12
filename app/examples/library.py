"""The example library: past replies Gamic answered, used to draft new ones.

An Example is a prospect's first reply and the reply Gamic actually sent. The library is stored in
the reply_examples table (source "history" for the past dataset, "ray_sent" for replies Ray sent
after we posted a draft) and read back here. New examples count immediately.

Selection (select_examples): the 5 closest, ordered by same label first, then same campaign
family, then most recent. The family is the first word after "GMC - " ("M&A" for
"GMC - M&A June (US/Canada)").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from app.db.client import Database, fetch_examples_for_label, fetch_recent_examples

K_EXAMPLES = 5
_FAMILY_PREFIX = re.compile(r"^\s*gmc\s*[-–]\s*", re.IGNORECASE)
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Example:
    source: str  # "history" or "ray_sent"
    source_thread_id: str
    campaign_name: str
    label: str
    prospect_reply: str
    gamic_reply: str
    replied_at: datetime | None = None


def campaign_family(campaign_name: str) -> str:
    """The first word after "GMC - ", upper-cased. Empty for an unknown campaign."""
    rest = _FAMILY_PREFIX.sub("", campaign_name or "").strip()
    return rest.split()[0].upper() if rest else ""


def select_examples(
    pool: list[Example],
    label: str,
    campaign_name: str,
    *,
    k: int = K_EXAMPLES,
    exclude_thread_id: str | None = None,
) -> list[Example]:
    """The k closest examples: same label, then same campaign family, then the most recent."""
    family = campaign_family(campaign_name)

    def closeness(ex: Example) -> tuple[bool, bool, datetime]:
        same_family = bool(family) and campaign_family(ex.campaign_name) == family
        return (ex.label == label, same_family, ex.replied_at or _EPOCH)

    usable = [
        ex
        for ex in pool
        if ex.gamic_reply.strip()
        and ex.prospect_reply.strip()
        and ex.source_thread_id != exclude_thread_id
    ]
    return sorted(usable, key=closeness, reverse=True)[:k]


def parse_time(value: Any) -> datetime | None:
    """An ISO time or an email-style date, as an aware UTC datetime; None if it cannot be read."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def example_from_row(row: dict[str, Any]) -> Example:
    return Example(
        source=row["source"],
        source_thread_id=str(row["source_thread_id"]),
        campaign_name=row.get("campaign_name") or "",
        label=row["label"],
        prospect_reply=row["prospect_reply"],
        gamic_reply=row["gamic_reply"],
        replied_at=parse_time(row.get("replied_at")),
    )


def load_pool(db: Database, label: str) -> list[Example]:
    """Candidates for a new reply: the newest examples of its label plus the newest overall."""
    rows = {r["id"]: r for r in [*fetch_examples_for_label(db, label), *fetch_recent_examples(db)]}
    return [example_from_row(r) for r in rows.values()]
