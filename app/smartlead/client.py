import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.config.settings import Settings
from app.db.suppressions import SuppressionScope
from app.smartlead.categories import Category, CategoryKind, load_categories
from app.smartlead.errors import CategoryWriteRefused, SendDisabledError, SmartleadError
from app.smartlead.models import Lead, Thread, UnsubscribeResult
from app.smartlead.thread import latest_reply_stats_id, parse_thread

# httpx logs full request URLs at INFO, and the API key travels in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_URL = "https://server.smartlead.ai/api/v1"
MAX_CAMPAIGN_IDS = 5
MAX_LIMIT = 20
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds."""

    def __init__(
        self,
        max_calls: int,
        period: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_calls = max_calls
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self._period:
                    self._calls.popleft()
                if len(self._calls) < self._max_calls:
                    self._calls.append(now)
                    return
                wait = self._period - (now - self._calls[0])
            self._sleep(max(wait, 0.0))


class SmartleadClient:
    def __init__(
        self,
        api_key: str,
        *,
        send_enabled: bool = False,
        category_writeback_enabled: bool = False,
        categories: Mapping[int, Category] | None = None,
        requests_per_minute: int = 120,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._api_key = api_key
        self._send_enabled = send_enabled
        self._category_writeback_enabled = category_writeback_enabled
        self._categories = categories if categories is not None else load_categories()
        self._sleep = sleep
        self._jitter = jitter
        self._limiter = RateLimiter(requests_per_minute, 60.0, clock, sleep)
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=30.0)

    @classmethod
    def from_settings(cls, settings: Settings) -> "SmartleadClient":
        return cls(
            settings.SMARTLEAD_API_KEY,
            send_enabled=settings.SEND_ENABLED,
            category_writeback_enabled=settings.CATEGORY_WRITEBACK_ENABLED,
        )

    def close(self) -> None:
        self._http.close()

    # -- low-level -----------------------------------------------------------------

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return self._jitter(0.0, min(BACKOFF_CAP, BACKOFF_BASE * 2**attempt))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        query = {"api_key": self._api_key, **(params or {})}
        last = "no response"
        for attempt in range(MAX_ATTEMPTS):
            self._limiter.acquire()
            response: httpx.Response | None = None
            try:
                response = self._http.request(method, path, params=query, json=json)
            except httpx.TransportError as exc:
                last = f"transport error: {type(exc).__name__}"
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    last = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise SmartleadError(
                        f"{method} {path} failed with HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                else:
                    return response.json() if response.content else None
            if attempt < MAX_ATTEMPTS - 1:
                self._sleep(self._backoff(attempt, response))
        raise SmartleadError(f"{method} {path} failed after {MAX_ATTEMPTS} attempts ({last})")

    # -- reading -------------------------------------------------------------------

    def fetch_master_inbox_replies(
        self,
        campaign_ids: list[int],
        limit: int = MAX_LIMIT,
        offset: int = 0,
        include_history: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= len(campaign_ids) <= MAX_CAMPAIGN_IDS:
            raise ValueError(f"campaign_ids must contain 1 to {MAX_CAMPAIGN_IDS} ids")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must not be negative")
        return self._request(
            "POST",
            "/master-inbox/inbox-replies",
            params={"fetch_message_history": "true"} if include_history else None,
            json={"offset": offset, "limit": limit, "filters": {"campaignId": campaign_ids}},
        )

    def find_inbox_item(
        self, campaign_id: int, lead_email: str, max_pages: int = 10
    ) -> dict[str, Any] | None:
        """The master-inbox item for a lead, found by email. Read only; None if not found."""
        target = lead_email.strip().lower()
        for page in range(max_pages):
            response = self.fetch_master_inbox_replies(
                [campaign_id], limit=MAX_LIMIT, offset=page * MAX_LIMIT
            )
            items = response.get("data") if isinstance(response, dict) else None
            if not isinstance(items, list):
                raise SmartleadError("unexpected master-inbox response shape")
            for item in items:
                if str(item.get("lead_email", "")).strip().lower() == target:
                    return item
            if len(items) < MAX_LIMIT:
                return None
        return None

    def get_thread_raw(self, lead: Lead) -> Any:
        return self._request(
            "GET", f"/campaigns/{lead.campaign_id}/leads/{lead.lead_id}/message-history"
        )

    def get_thread(self, lead: Lead) -> Thread:
        payload = self.get_thread_raw(lead)
        messages = parse_thread(payload)
        return Thread(
            lead=lead,
            messages=messages,
            reply_to_stats_id=latest_reply_stats_id(messages),
            to_email=lead.email,
        )

    def get_lead_categories(self) -> Any:
        return self._request("GET", "/leads/fetch-categories")

    # -- acting --------------------------------------------------------------------

    def send_reply(self, thread: Thread, body: str) -> Any:
        if not self._send_enabled:
            raise SendDisabledError("SEND_ENABLED is false; refusing to send a reply")
        if not thread.reply_to_stats_id:
            raise SmartleadError("thread has no reply_to_stats_id; cannot send a reply")
        payload: dict[str, Any] = {
            "email_stats_id": thread.reply_to_stats_id,
            "email_body": body,
        }
        if thread.to_email:
            payload["to_email"] = thread.to_email
        return self._request(
            "POST", f"/campaigns/{thread.lead.campaign_id}/reply-email-thread", json=payload
        )

    def unsubscribe_lead(
        self,
        lead: Lead,
        scope: SuppressionScope | str = SuppressionScope.CAMPAIGN,
        *,
        confirmed: bool = False,
    ) -> UnsubscribeResult:
        """Unsubscribe at campaign scope by default.

        Global scope only runs with confirmed=True (a human confirmed in Slack).
        Client scope is not a Smartlead route; the caller unsubscribes per campaign.
        """
        scope = SuppressionScope(scope)
        if scope is SuppressionScope.CLIENT:
            raise ValueError(
                "client scope is not supported by Smartlead; unsubscribe each campaign instead"
            )
        if scope is SuppressionScope.GLOBAL:
            if not confirmed:
                raise ValueError("global unsubscribe requires confirmed=True")
            path = f"/leads/{lead.lead_id}/unsubscribe"
        else:
            path = f"/campaigns/{lead.campaign_id}/leads/{lead.lead_id}/unsubscribe"
        self._request("POST", path)
        return UnsubscribeResult(
            scope=scope, campaign_id=lead.campaign_id, lead_id=lead.lead_id, ok=True
        )

    def pause_lead(self, lead: Lead) -> None:
        self._request("POST", f"/campaigns/{lead.campaign_id}/leads/{lead.lead_id}/pause")

    def update_lead_category(self, lead: Lead, category_id: int) -> None:
        """Write a reply-label category back to Smartlead.

        Refused, before any HTTP call, unless SEND_ENABLED and CATEGORY_WRITEBACK_ENABLED are
        both on and the category is a reply_label. Workflow triggers and unknown categories
        are never written: they can fire Smartlead automations.
        """
        category = self._categories.get(category_id)
        if category is None or category.kind is not CategoryKind.REPLY_LABEL:
            kind = category.kind.value if category else "not in smartlead_categories.yaml"
            raise CategoryWriteRefused(
                f"category {category_id} is not a reply_label ({kind}); refusing to write it"
            )
        if not self._send_enabled:
            raise CategoryWriteRefused("SEND_ENABLED is false; refusing category write-back")
        if not self._category_writeback_enabled:
            raise CategoryWriteRefused("CATEGORY_WRITEBACK_ENABLED is false; refusing write-back")
        method, path, body = _build_category_request(lead, category_id)
        self._request(method, path, json=body)


def _build_category_request(lead: Lead, category_id: int) -> tuple[str, str, dict[str, Any]]:
    """Return (method, path, body) for assigning a category to a lead.

    Known parameters only:
    - per-campaign update: campaign_id, lead_id, category_id (null clears it) and an
      optional pause_lead.
    - master-inbox update: email_lead_map_id and category_id.

    The HTTP method and path are UNVERIFIED. Smartlead's current docs do not document a
    per-lead assignment endpoint. Do not implement this from a third-party guess; fill it in
    only from an official Smartlead page or a confirmed real response.
    """
    raise NotImplementedError(
        "update_lead_category endpoint not confirmed; fill in _build_category_request"
    )
