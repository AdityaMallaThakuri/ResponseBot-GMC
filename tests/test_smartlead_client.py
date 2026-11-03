import json
from pathlib import Path

import httpx
import pytest

from app.db.suppressions import SuppressionScope
from app.smartlead.client import MAX_ATTEMPTS, RateLimiter, SmartleadClient
from app.smartlead.errors import SendDisabledError, SmartleadError
from app.smartlead.models import Direction, Lead, Thread

FIXTURES = Path(__file__).parent / "fixtures"
LEAD = Lead(campaign_id=101, lead_id=555, email="jane@example.com")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class Recorder:
    """Mock transport that records requests and replays queued responses."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def make_client(*responses: httpx.Response, send_enabled: bool = False, **kw):
    rec = Recorder(*responses)
    sleeps: list[float] = []
    client = SmartleadClient(
        "secret-key",
        send_enabled=send_enabled,
        send_allowlist=kw.pop("send_allowlist", frozenset({"jane@example.com"})),
        transport=httpx.MockTransport(rec),
        sleep=sleeps.append,
        jitter=lambda lo, hi: hi,
        **kw,
    )
    return client, rec, sleeps


def ok(body: object = None) -> httpx.Response:
    return httpx.Response(200, json=body if body is not None else {"ok": True})


# -- fetching ----------------------------------------------------------------------


def test_fetch_replies_sends_filters_and_key() -> None:
    client, rec, _ = make_client(ok(load("inbox_replies.json")))
    data = client.fetch_master_inbox_replies([101, 102], limit=20, offset=40)
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path.endswith("/master-inbox/inbox-replies")
    assert req.url.params["api_key"] == "secret-key"
    assert json.loads(req.content) == {
        "offset": 40,
        "limit": 20,
        "filters": {"campaignId": [101, 102]},
    }
    assert data["data"][0]["lead_email"] == "jane@example.com"


def test_fetch_replies_can_include_history() -> None:
    client, rec, _ = make_client(ok({"messages": []}))
    client.fetch_master_inbox_replies([101], limit=3, include_history=True)
    assert rec.requests[0].url.params["fetch_message_history"] == "true"


def test_get_lead_categories_is_a_get() -> None:
    client, rec, _ = make_client(ok([{"id": 1, "name": "Interested"}]))
    assert client.get_lead_categories()[0]["name"] == "Interested"
    assert rec.requests[0].method == "GET"
    assert rec.requests[0].url.path.endswith("/leads/fetch-categories")


@pytest.mark.parametrize(
    ("ids", "limit", "offset"),
    [([], 20, 0), (list(range(6)), 20, 0), ([1], 21, 0), ([1], 0, 0), ([1], 20, -1)],
)
def test_fetch_replies_enforces_limits(ids: list[int], limit: int, offset: int) -> None:
    client, rec, _ = make_client(ok({}))
    with pytest.raises(ValueError):
        client.fetch_master_inbox_replies(ids, limit, offset)
    assert rec.requests == []


def test_get_thread_returns_clean_ordered_messages() -> None:
    client, rec, _ = make_client(ok(load("message_history.json")))
    thread = client.get_thread(LEAD)
    assert rec.requests[0].url.path.endswith("/campaigns/101/leads/555/message-history")
    assert [m.direction for m in thread.messages] == [
        Direction.SENT,
        Direction.REPLY,
        Direction.SENT,
    ]
    assert thread.messages[0].text == "Jane - saw Acme Advisory closed a deal.\n\nWorth a chat?"
    assert thread.messages[1].text == "Sounds interesting, can you send times?"
    assert thread.messages[1].timestamp.isoformat() == "2026-10-20T14:00:00+00:00"
    # the reply target is the stats_id of the lead's latest reply
    assert [m.message_id for m in thread.messages][1] == "<reply-1@example.com>"
    assert thread.reply_to_stats_id == "11111111-aaaa-4aaa-8aaa-000000000001"
    assert thread.to_email == "jane@example.com"


# -- send guard ---------------------------------------------------------------------


def test_send_reply_refused_when_disabled() -> None:
    client, rec, _ = make_client(ok())
    thread = Thread(lead=LEAD, reply_to_stats_id="abc")
    with pytest.raises(SendDisabledError):
        client.send_reply(thread, "hi")
    assert rec.requests == []


def test_send_reply_posts_when_enabled() -> None:
    client, rec, _ = make_client(ok(), send_enabled=True)
    client.send_reply(Thread(lead=LEAD, reply_to_stats_id="abc", to_email="jane@example.com"), "hi")
    req = rec.requests[0]
    assert req.url.path.endswith("/campaigns/101/reply-email-thread")
    assert json.loads(req.content) == {
        "email_stats_id": "abc",
        "email_body": "hi",
        "to_email": "jane@example.com",
    }


def test_send_reply_needs_stats_id() -> None:
    client, _, _ = make_client(ok(), send_enabled=True)
    with pytest.raises(SmartleadError):
        client.send_reply(Thread(lead=LEAD), "hi")


# -- unsubscribe / pause / category -------------------------------------------------


def test_unsubscribe_defaults_to_campaign_scope() -> None:
    client, rec, _ = make_client(ok())
    result = client.unsubscribe_lead(LEAD)
    assert rec.requests[0].url.path.endswith("/campaigns/101/leads/555/unsubscribe")
    assert result.scope is SuppressionScope.CAMPAIGN and result.ok


def test_global_unsubscribe_requires_confirmation() -> None:
    client, rec, _ = make_client(ok())
    with pytest.raises(ValueError, match="confirmed=True"):
        client.unsubscribe_lead(LEAD, scope="global")
    assert rec.requests == []


def test_global_unsubscribe_when_confirmed() -> None:
    client, rec, _ = make_client(ok())
    result = client.unsubscribe_lead(LEAD, scope="global", confirmed=True)
    assert rec.requests[0].url.path.endswith("/leads/555/unsubscribe")
    assert "/campaigns/" not in rec.requests[0].url.path
    assert result.scope is SuppressionScope.GLOBAL


def test_client_scope_not_supported() -> None:
    client, rec, _ = make_client(ok())
    with pytest.raises(ValueError, match="client scope"):
        client.unsubscribe_lead(LEAD, scope="client")
    assert rec.requests == []


def test_pause_lead() -> None:
    client, rec, _ = make_client(ok())
    client.pause_lead(LEAD)
    assert rec.requests[0].url.path.endswith("/campaigns/101/leads/555/pause")


# -- retry and rate limit -----------------------------------------------------------


def test_retries_429_then_succeeds_honouring_retry_after() -> None:
    client, rec, sleeps = make_client(
        httpx.Response(429, headers={"Retry-After": "7"}), ok({"fine": 1})
    )
    assert client.pause_lead(LEAD) is None
    assert len(rec.requests) == 2 and sleeps == [7.0]


def test_retries_5xx_with_exponential_backoff_then_errors() -> None:
    client, rec, sleeps = make_client(httpx.Response(503, text="down"))
    with pytest.raises(SmartleadError, match=f"after {MAX_ATTEMPTS} attempts"):
        client.pause_lead(LEAD)
    assert len(rec.requests) == MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_client_error_is_not_retried_and_hides_key() -> None:
    client, rec, _ = make_client(httpx.Response(401, json={"message": "Invalid API Key"}))
    with pytest.raises(SmartleadError) as exc:
        client.pause_lead(LEAD)
    assert len(rec.requests) == 1
    assert "secret-key" not in str(exc.value)


def test_rate_limiter_waits_when_window_full() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(2, 60.0, clock=lambda: now[0], sleep=fake_sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert sleeps == [60.0]
