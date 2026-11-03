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


@dataclass
class IngestDeps:
    db: Database
    smartlead: Any  # SmartleadClient, or a fake with get_thread_raw and find_inbox_item
    clock: Callable[[], datetime] = field(default=utc_now)


def build_deps(settings: Settings) -> IngestDeps:
    """The real database and Smartlead client. Ingest only ever reads from Smartlead."""
    from app.db.connect import create_db
    from app.smartlead.client import SmartleadClient

    return IngestDeps(db=create_db(settings), smartlead=SmartleadClient.from_settings(settings))
