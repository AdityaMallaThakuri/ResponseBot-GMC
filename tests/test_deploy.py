import httpx
import pytest
from fastapi.testclient import TestClient

from app.config.settings import get_settings
from app.smartlead.client import SmartleadClient
from scripts import attach_webhooks as aw

BASE = "https://bot.example.app"


def make_client(handler) -> SmartleadClient:
    return SmartleadClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None)


def test_gmc_names() -> None:
    assert aw.is_gmc("GMC - Lenders") and aw.is_gmc("gmc x") and not aw.is_gmc("Client - GMC")


def test_gather_picks_children_by_parent_id_and_excludes_client_campaigns() -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        if req.url.path.endswith("/campaigns/"):
            return httpx.Response(200, json=[
                {"id": 1, "name": "GMC - A", "status": "ACTIVE"},
                # child by parent id; the name does not say POS REPLY
                {"id": 2, "name": "A follow-up", "parent_campaign_id": 1},
                # named POS REPLY but its parent is not a GMC campaign: not counted
                {"id": 3, "name": "GMC - B POS REPLY", "parent_campaign_id": 99},
                {"id": 4, "name": "Other POS REPLY", "parent_campaign_id": 99},
                # a client campaign named GMC, and a child of it
                {"id": 5, "name": "GMC - C", "client_id": 7},
                {"id": 6, "name": "C POS REPLY", "parent_campaign_id": 5},
                # a child of a GMC campaign that has a client_id itself
                {"id": 7, "name": "GMC - A kid", "parent_campaign_id": 1, "client_id": 3},
                {"id": 8, "name": "Unrelated"},
            ])
        if req.url.path.endswith("/1/webhooks"):
            ours = f"{BASE}/webhooks/smartlead?secret=x"
            return httpx.Response(200, json=[{"webhook_url": ours}])
        return httpx.Response(200, json=[{"webhook_url": "https://other.example/hook"}])

    rows = aw.gather(make_client(handler), BASE)
    got = [(r.id, r.pos_reply, r.parent_name, r.has_ours) for r in rows]
    # id 3 is GMC-named so it is a GMC campaign itself, not a child; the others are excluded
    assert got == [(1, False, "", True), (2, True, "GMC - A", False), (3, False, "", False)]
    assert all(m == "GET" for m in calls)  # read only


def test_apply_creates_only_where_missing_and_only_one_event() -> None:
    posted = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append((req.method, req.url.path, req.content))
        return httpx.Response(200, json={"ok": True, "id": 9})

    rows = [
        aw.CampaignRow(1, "GMC - A", "", False, True),
        aw.CampaignRow(2, "GMC - B", "", False, False),
    ]
    n = aw.apply(make_client(handler), rows, aw.webhook_url(BASE, "s3cret"))
    assert n == 1 and len(posted) == 1
    method, path, body = posted[0]
    assert (method, path.endswith("/webhook/create")) == ("POST", True)
    assert b'"EMAIL_REPLY":true' in body.replace(b" ", b"") and b'"campaign"' in body
    assert b"force_create\":false" in body.replace(b" ", b"")


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
