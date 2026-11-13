"""Campaign verticals: which kind of campaign a reply belongs to (app/config/verticals.yaml).

The hard line is M&A against everything else: an example or SOP template from an M&A campaign is
never used to draft a reply for any other campaign, and the other way round. A campaign that no
rule places is "unknown" and counts as not M&A.

A child campaign (POS REPLY, No-Show) takes the vertical of its root parent, found through
`parent_campaign_id` in Smartlead's campaign list.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "verticals.yaml"
MA = "ma"
UNKNOWN = "unknown"
VERTICALS = ("ma", "lenders", "marketing", "relocation", "solar", "facilities", "other", UNKNOWN)
CACHE_SECONDS = 600.0


def is_ma(vertical: str | None) -> bool:
    return (vertical or "") == MA


@dataclass(frozen=True)
class Rules:
    campaign_ids: dict[int, str]
    overrides: tuple[tuple[str, str], ...]  # (lower-case fragment, vertical)
    rules: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]  # (vertical, patterns), in order


def _check(vertical: Any) -> str:
    if vertical not in VERTICALS:
        raise ValueError(f"verticals.yaml: unknown vertical {vertical!r}")
    return str(vertical)


def parse_rules(data: dict[str, Any]) -> Rules:
    ids = {int(k): _check(v) for k, v in (data.get("campaign_ids") or {}).items()}
    overrides = tuple(
        (str(o["contains"]).strip().lower(), _check(o["vertical"]))
        for o in data.get("overrides") or []
    )
    rules = tuple(
        (
            _check(r["vertical"]),
            tuple(re.compile(p, re.IGNORECASE) for p in r.get("patterns") or []),
        )
        for r in data.get("rules") or []
    )
    return Rules(ids, overrides, rules)


def load_rules(path: Path = CONFIG_PATH) -> Rules:
    return parse_rules(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def classify_name(rules: Rules, name: str, campaign_id: int | None = None) -> str:
    """The vertical of one campaign by id, override or rule. "unknown" when nothing matches."""
    if campaign_id is not None and campaign_id in rules.campaign_ids:
        return rules.campaign_ids[campaign_id]
    lowered = (name or "").lower()
    for fragment, vertical in rules.overrides:
        if fragment and fragment in lowered:
            return vertical
    for vertical, patterns in rules.rules:
        if any(p.search(name or "") for p in patterns):
            return vertical
    return UNKNOWN


def root_campaign(campaigns: list[dict[str, Any]], campaign_id: int) -> dict[str, Any] | None:
    """The campaign at the top of the parent chain (itself if it has no parent). None if the id
    is not in the list. A parent loop stops at the first repeat."""
    by_id = {int(c["id"]): c for c in campaigns}
    current = by_id.get(campaign_id)
    seen: set[int] = set()
    while current is not None:
        seen.add(int(current["id"]))
        parent = current.get("parent_campaign_id")
        if parent in (None, "", 0) or int(parent) in seen or int(parent) not in by_id:
            return current
        current = by_id[int(parent)]
    return None


def vertical_of_campaign(
    rules: Rules, campaigns: list[dict[str, Any]], campaign_id: int | None, name: str = ""
) -> str:
    """Resolve through the root parent when the campaign list knows the id, else by name."""
    if campaign_id is not None:
        root = root_campaign(campaigns, campaign_id)
        if root is not None:
            return classify_name(rules, str(root.get("name") or ""), int(root["id"]))
    return classify_name(rules, name, campaign_id)


class VerticalResolver:
    """Resolves a campaign id (and name) to a vertical from a cached Smartlead campaign list.

    `campaigns` returns the list (SmartleadClient.list_campaigns). If it cannot be read, the name
    rules alone are used, so a draft never fails because of this.
    """

    def __init__(
        self,
        campaigns: Callable[[], list[dict[str, Any]]] | None = None,
        rules: Rules | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_SECONDS,
    ) -> None:
        self._source = campaigns
        self.rules = rules or load_rules()
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: list[dict[str, Any]] | None = None
        self._fetched_at = 0.0

    def _list(self, refresh: bool = False) -> list[dict[str, Any]]:
        if self._source is None:
            return []
        with self._lock:
            stale = self._clock() - self._fetched_at >= self._ttl
            if self._cache is None or stale or refresh:
                try:
                    self._cache = list(self._source())
                    self._fetched_at = self._clock()
                except Exception as exc:  # noqa: BLE001 - fall back to names
                    logger.warning("vertical: campaign list unavailable: %s", type(exc).__name__)
            return self._cache or []

    def for_campaign(self, campaign_id: int | None, name: str = "") -> str:
        campaigns = self._list()
        if campaign_id is not None and campaign_id not in {int(c["id"]) for c in campaigns}:
            campaigns = self._list(refresh=True)  # an unknown id: look once more
        return vertical_of_campaign(self.rules, campaigns, campaign_id, name)

    def for_thread(self, thread: dict[str, Any]) -> str:
        """The thread's stored vertical, else resolved from its campaign id and name."""
        stored = str(thread.get("vertical") or "")
        if stored in VERTICALS:
            return stored
        raw = thread.get("email_campaign_id")
        campaign_id = int(raw) if str(raw or "").isdigit() else None
        return self.for_campaign(campaign_id, str(thread.get("campaign_name") or ""))
