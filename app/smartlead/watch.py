"""Which Smartlead campaigns the bot watches, and keeping our webhook on them.

Rule (automatic discovery): a campaign is watched if its name starts with "GMC" and it has no
client_id, or its parent_campaign_id is a watched campaign (a POS REPLY child; the name is not
used) and it has no client_id itself. IGNORE_CAMPAIGN_IDS are then removed. If GMC_CAMPAIGN_IDS
is set it overrides the rule: only those ids are watched (minus the ignored ones).

Nothing here sends mail. The only write is `create_campaign_webhook` (an EMAIL_REPLY webhook with
?secret=), and only when AUTO_ATTACH_WEBHOOKS is on. No other webhook is ever updated or deleted.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PATH = "/webhooks/smartlead"
WEBHOOK_NAME = "Reply Bot (EMAIL_REPLY)"
CACHE_SECONDS = 600.0  # the campaign list is cached for 10 minutes


@dataclass(frozen=True)
class Watched:
    id: int
    name: str
    status: str = ""
    parent_name: str = ""  # set for a POS REPLY child: the name of its parent


def is_gmc(name: str) -> bool:
    return name.strip().lower().startswith("gmc")


def has_client(campaign: dict[str, Any]) -> bool:
    return campaign.get("client_id") not in (None, "", 0)


def parent_id(campaign: dict[str, Any]) -> int | None:
    value = campaign.get("parent_campaign_id")
    return int(value) if value not in (None, "", 0) else None


def webhook_url(base_url: str, secret: str) -> str:
    return f"{base_url.rstrip('/')}{PATH}?secret={secret}"


def is_ours(hook: dict[str, Any], base_url: str) -> bool:
    """Our webhook: same host and path as base_url (the secret is ignored in the comparison)."""
    url = str(hook.get("webhook_url") or "")
    return url.split("?")[0].rstrip("/") == f"{base_url.rstrip('/')}{PATH}"


def watched_campaigns(
    campaigns: list[dict[str, Any]],
    override: frozenset[int] = frozenset(),
    ignore: frozenset[int] = frozenset(),
) -> list[Watched]:
    by_id = {int(c["id"]): c for c in campaigns}
    if override:
        out = []
        for cid in sorted(override - ignore):
            c = by_id.get(cid, {})
            name = str(c.get("name") or f"campaign {cid}")
            out.append(Watched(cid, name, str(c.get("status") or "")))
        return out
    eligible = {cid: c for cid, c in by_id.items() if not has_client(c)}
    watched: dict[int, Watched] = {
        cid: Watched(cid, str(c.get("name") or ""), str(c.get("status") or ""))
        for cid, c in eligible.items()
        if is_gmc(str(c.get("name") or ""))
    }
    changed = True
    while changed:  # children, and children of children
        changed = False
        for cid, c in eligible.items():
            pid = parent_id(c)
            if cid not in watched and pid in watched:
                watched[cid] = Watched(
                    cid, str(c.get("name") or ""), str(c.get("status") or ""), watched[pid].name
                )
                changed = True
    return sorted(
        (w for w in watched.values() if w.id not in ignore),
        key=lambda w: (w.parent_name or w.name, bool(w.parent_name), w.id),
    )


class CampaignWatcher:
    """Answers "is this campaign watched?" from a cached campaign list (10 minutes)."""

    def __init__(
        self,
        client: Any,
        settings_provider: Callable[[], Any],
        clock: Callable[[], float] = time.monotonic,
        ttl: float = CACHE_SECONDS,
    ) -> None:
        self._client = client
        self._settings = settings_provider
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._campaigns: list[dict[str, Any]] | None = None
        self._fetched_at = 0.0

    def _refresh(self) -> list[dict[str, Any]]:
        self._campaigns = list(self._client.list_campaigns())
        self._fetched_at = self._clock()
        return self._campaigns

    def is_watched(self, campaign_id: int) -> bool:
        """Raises SmartleadError only when the list is needed and cannot be fetched."""
        settings = self._settings()
        ignore = settings.ignore_campaign_ids
        override = settings.gmc_campaign_ids
        if override:  # testing override: no Smartlead call at all
            return campaign_id in override and campaign_id not in ignore
        with self._lock:
            fetched_now = False
            campaigns = self._campaigns
            if campaigns is None or self._clock() - self._fetched_at >= self._ttl:
                campaigns = self._refresh()
                fetched_now = True
            if campaign_id not in {int(c["id"]) for c in campaigns} and not fetched_now:
                campaigns = self._refresh()  # an unknown id: look once more before ignoring it
            return any(
                w.id == campaign_id for w in watched_campaigns(campaigns, frozenset(), ignore)
            )


@dataclass(frozen=True)
class AttachResult:
    attached: tuple[str, ...] = ()
    would_attach: tuple[str, ...] = ()
    failed: int = 0
    skipped: str = ""  # why nothing was done at all


class AttachJob:
    """List campaigns, find watched ones without our webhook, attach it (if allowed)."""

    def __init__(
        self,
        client: Any,
        settings_provider: Callable[[], Any],
        post_note: Callable[[str], None],
    ) -> None:
        self._client = client
        self._settings = settings_provider
        self._post_note = post_note
        self._announced: set[int] = set()  # one "would watch" note per campaign per process

    def _note(self, text: str) -> None:
        try:
            self._post_note(text)
        except Exception as exc:  # noqa: BLE001 - a Slack problem must not stop the job
            logger.warning("webhook job: could not post to Slack: %s", type(exc).__name__)

    def run_once(self) -> AttachResult:
        settings = self._settings()
        base = settings.PUBLIC_BASE_URL.strip()
        if not base:
            logger.warning("webhook job: PUBLIC_BASE_URL is not set; nothing to do")
            return AttachResult(skipped="PUBLIC_BASE_URL not set")
        campaigns = self._client.list_campaigns()
        watched = watched_campaigns(
            campaigns, settings.gmc_campaign_ids, settings.ignore_campaign_ids
        )
        missing = [
            w
            for w in watched
            if not any(is_ours(h, base) for h in self._client.list_campaign_webhooks(w.id))
        ]
        attach = settings.AUTO_ATTACH_WEBHOOKS
        logger.info(
            "webhook job: %d watched, %d without our webhook, auto attach %s",
            len(watched),
            len(missing),
            "on" if attach else "off (report only)",
        )
        attached: list[str] = []
        would: list[str] = []
        failed = 0
        url = webhook_url(base, settings.WEBHOOK_SECRET)
        for w in missing:
            if not attach:
                would.append(w.name)
                logger.info("webhook job: would attach to %s (%d)", w.name, w.id)
                if w.id not in self._announced:
                    self._announced.add(w.id)
                    self._note(f"Would start watching (AUTO_ATTACH_WEBHOOKS is off): {w.name}")
                continue
            try:
                self._client.create_campaign_webhook(w.id, WEBHOOK_NAME, url)
            except Exception as exc:  # noqa: BLE001 - try again next run
                failed += 1
                logger.error("webhook job: could not attach to %d: %s", w.id, type(exc).__name__)
                continue
            attached.append(w.name)
            logger.info("webhook job: attached to %s (%d)", w.name, w.id)
            self._note(f"Now watching: {w.name}")
        return AttachResult(tuple(attached), tuple(would), failed)
