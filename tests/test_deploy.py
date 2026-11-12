import httpx
import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.smartlead.client import SmartleadClient
from scripts import attach_webhooks as aw

BASE = "https://bot.example.app"
OURS = f"{BASE}/webhooks/smartlead?secret=x"


def make_client(handler) -> SmartleadClient:
    return SmartleadClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_gather_marks_children_and_existing_hooks() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"  # read only
        if req.url.path.endswith("/campaigns/"):
            return httpx.Response(200, json=[
                {"id": 1, "name": "GMC - A"},
                {"id": 2, "name": "A follow-up", "parent_campaign_id": 1},
                {"id": 5, "name": "GMC - C", "client_id": 7},
                {"id": 6, "name": "C POS REPLY", "parent_campaign_id": 5},
            ])
        if req.url.path.endswith("/1/webhooks"):
            return httpx.Response(200, json=[{"id": 11, "webhook_url": OURS}])
        return httpx.Response(200, json=[{"id": 12, "webhook_url": "https://other.example/h"}])

    rows = aw.gather(make_client(handler), BASE, frozenset(), frozenset())
    got = [(r.id, r.campaign.parent_name, r.has_ours) for r in rows]
    assert got == [(1, "", True), (2, "GMC - A", False)]


def test_apply_creates_only_where_missing_and_only_one_event() -> None:
    posted = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append((req.method, req.url.path, req.content))
        return httpx.Response(200, json={"ok": True, "id": 9})

    rows = [
        aw.CampaignRow(aw.Watched(1, "GMC - A"), True),
        aw.CampaignRow(aw.Watched(2, "GMC - B"), False),
    ]
    n = aw.apply(make_client(handler), rows, aw.webhook_url(BASE, "s3cret"))
    assert n == 1 and len(posted) == 1
    method, path, body = posted[0]
    compact = body.replace(b" ", b"")
    assert (method, path.endswith("/webhook/create")) == ("POST", True)
    assert b'"EMAIL_REPLY":true' in compact and b'"campaign"' in compact
    assert b'"force_create":false' in compact


def test_detach_removes_only_our_webhooks_by_id() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path, req.content))
        if req.method == "GET":
            return httpx.Response(200, json=[
                {"id": 11, "webhook_url": OURS},
                {"id": 12, "webhook_url": "https://other.example/webhooks/smartlead?secret=x"},
                {"id": 13, "webhook_url": f"{BASE}/other"},
            ])
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    found = aw.find_ours(client, [1], BASE)
    assert [(c, h["id"]) for c, h in found] == [(1, 11)]  # a dry run stops here: only GETs so far
    assert {m for m, _, _ in calls} == {"GET"}
    assert aw.detach(client, found, BASE) == 1
    deletes = [c for c in calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and b"11" in deletes[0][2]


def test_detach_uses_the_other_delete_route_if_the_first_fails() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(404 if req.url.path.endswith("/delete") else 200, json={})

    make_client(handler).delete_webhook(11)
    assert calls == [("DELETE", "/api/v1/webhook/delete"), ("DELETE", "/api/v1/webhook/delete/11")]


def test_production_refuses_to_start_with_a_send_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("SEND_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="must be false"), TestClient(app):
            pass
    finally:
        get_settings.cache_clear()
