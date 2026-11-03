"""Tests for the Smartlead webhook receiver and the processing of stored reply events."""

import copy
import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config.settings import ConfigError, get_settings, parse_campaign_ids
from app.ingest import deps as deps_module
from app.ingest import process as process_module
from app.ingest import webhook as webhook_module
from app.ingest.deps import IngestDeps
from app.ingest.process import FAILED, FIRST_REPLY, FOLLOW_UP, process_reply_event
from app.ingest.webhook import event_key
from app.main import app
from app.smartlead.errors import SmartleadError
from tests.conftest import REQUIRED as GOOD_VALUES
from tests.fakedb import FakeDb

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = GOOD_VALUES["WEBHOOK_SECRET"]
URL = "/webhooks/smartlead"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MARKER = "TOPSECRET-MARKER-TEXT"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


EVENT = fixture("webhook_email_reply.json")
HISTORY = fixture("message_history.json")
INBOX_ITEM = fixture("inbox_replies.json")["data"][0]


class FakeSmartlead:
    """Read-only stand-in for SmartleadClient. Records calls; only reads exist."""

    def __init__(self, history: Any = None, item: Any = INBOX_ITEM) -> None:
        self.history = history if history is not None else copy.deepcopy(HISTORY)
        self.item = item
        self.thread_calls: list[Any] = []
        self.lookups: list[tuple[int, str]] = []
        self.error: Exception | None = None

    def get_thread_raw(self, lead: Any) -> Any:
        self.thread_calls.append(lead)
        if self.error:
            raise self.error
        return self.history

    def find_inbox_item(self, campaign_id: int, email: str) -> Any:
        self.lookups.append((campaign_id, email))
        return self.item


def sign(body: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Smartlead-Signature": f"sha256={digest}", "Content-Type": "application/json"}


@pytest.fixture
def smartlead() -> FakeSmartlead:
    return FakeSmartlead()


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, db: FakeDb, smartlead: FakeSmartlead
) -> Iterator[TestClient]:
    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GMC_CAMPAIGN_IDS", "101, 102")
    get_settings.cache_clear()
    app.state.ingest = IngestDeps(db=db, smartlead=smartlead, clock=lambda: NOW)
    yield TestClient(app)
    del app.state.ingest
    get_settings.cache_clear()


def post(client: TestClient, payload: dict[str, Any], **headers: str) -> Any:
    body = json.dumps(payload).encode()
    return client.post(URL, content=body, headers=headers or sign(body))


def rows(db: FakeDb, table: str) -> list[dict[str, Any]]:
    return db.tables.get(table, [])


# ------------------------------------------------------------------ authentication


def test_bad_secret_is_rejected_and_the_body_is_not_logged(
    client: TestClient, db: FakeDb, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {**EVENT, "subject": MARKER}
    body = json.dumps(payload).encode()
    response = client.post(URL, content=body, headers=sign(body, "x" * 40))
    assert response.status_code == 401
    assert rows(db, "raw_events") == []
    assert MARKER not in caplog.text and MARKER not in response.text


def test_missing_secret_is_rejected(client: TestClient, db: FakeDb) -> None:
    response = client.post(URL, json=EVENT)
    assert response.status_code == 401 and rows(db, "raw_events") == []


@pytest.mark.parametrize(
    "headers",
    [{"X-Webhook-Secret": "wrong"}, {"X-Smartlead-Signature": "sha256=" + "0" * 64}, {}],
)
def test_wrong_header_credentials_are_rejected(
    client: TestClient, db: FakeDb, headers: dict[str, str]
) -> None:
    response = client.post(URL, json=EVENT, headers=headers)
    assert response.status_code == 401 and rows(db, "raw_events") == []


def test_a_bad_signature_is_not_rescued_by_a_valid_body_secret(
    client: TestClient, db: FakeDb
) -> None:
    body = json.dumps({**EVENT, "secret_key": SECRET}).encode()
    response = client.post(URL, content=body, headers=sign(body, "y" * 40))
    assert response.status_code == 401 and rows(db, "raw_events") == []


def test_each_accepted_way_of_presenting_the_secret_works(client: TestClient) -> None:
    assert post(client, {**EVENT, "time_replied": "2026-10-20T14:00:01Z"}).status_code == 200
    assert (
        post(
            client,
            {**EVENT, "time_replied": "2026-10-20T14:00:02Z"},
            **{"X-Webhook-Secret": SECRET},
        ).status_code
        == 200
    )
    by_body = {**EVENT, "time_replied": "2026-10-20T14:00:03Z", "secret_key": SECRET}
    assert client.post(URL, json=by_body).status_code == 200
    wrong_body = {**EVENT, "time_replied": "2026-10-20T14:00:04Z", "secret_key": "no"}
    assert client.post(URL, json=wrong_body).status_code == 401


def test_the_secret_is_never_stored(client: TestClient, db: FakeDb) -> None:
    assert client.post(URL, json={**EVENT, "secret_key": SECRET}).status_code == 200
    stored = rows(db, "raw_events")[0]["payload"]
    assert "secret_key" not in stored and SECRET not in json.dumps(rows(db, "raw_events"))


def test_unreadable_body_with_a_valid_secret_is_a_400(client: TestClient, db: FakeDb) -> None:
    headers = {"X-Webhook-Secret": SECRET}
    assert client.post(URL, content=b"not json", headers=headers).status_code == 400
    assert client.post(URL, content=b"[1, 2]", headers=headers).status_code == 400
    assert rows(db, "raw_events") == []


# ------------------------------------------------------------------ filtering


def test_other_event_types_are_ignored_with_a_log_line(
    client: TestClient, db: FakeDb, smartlead: FakeSmartlead, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    for event_type in (
        "EMAIL_SENT",
        "EMAIL_BOUNCE",
        "LEAD_UNSUBSCRIBED",
        "UNTRACKED_REPLIES",
        None,
    ):
        response = post(client, {**EVENT, "event_type": event_type})
        assert response.status_code == 200 and response.json()["reason"] == "event_type"
    assert rows(db, "raw_events") == [] and smartlead.thread_calls == []
    assert "ignored: event_type=EMAIL_SENT" in caplog.text


def test_campaigns_outside_the_list_are_ignored_with_a_log_line(
    client: TestClient, db: FakeDb, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    for campaign in (999, "abc", None):
        response = post(client, {**EVENT, "campaign_id": campaign})
        assert response.status_code == 200 and response.json()["reason"] == "campaign"
    assert rows(db, "raw_events") == []
    assert "campaign_id=999 not in GMC_CAMPAIGN_IDS" in caplog.text
    assert post(client, {**EVENT, "campaign_id": "102"}).json()["status"] == "accepted"


def test_an_empty_campaign_list_ignores_everything(
    client: TestClient, db: FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GMC_CAMPAIGN_IDS", "")
    get_settings.cache_clear()
    assert post(client, EVENT).json()["reason"] == "campaign"
    assert rows(db, "raw_events") == []


# ------------------------------------------------------------------ storing and dedupe


def test_a_reply_is_stored_then_processed(client: TestClient, db: FakeDb) -> None:
    response = post(client, EVENT)
    assert response.status_code == 200 and response.json() == {"status": "accepted"}
    event = rows(db, "raw_events")[0]
    assert event["source"] == "smartlead_webhook" and event["payload"]["campaign_id"] == 101
    assert event["processed"] is True and "process_error" not in event
    assert len(rows(db, "threads")) == 1 and len(rows(db, "messages")) == 3


def test_a_duplicate_delivery_is_stored_and_processed_once(
    client: TestClient, db: FakeDb, smartlead: FakeSmartlead
) -> None:
    assert post(client, EVENT).json()["status"] == "accepted"
    assert post(client, EVENT).json() == {"status": "duplicate"}
    redelivered = {**EVENT, "webhook_id": 7, "secret_key": SECRET}  # retry with other extras
    assert client.post(URL, json=redelivered).json() == {"status": "duplicate"}
    assert len(rows(db, "raw_events")) == 1 and len(smartlead.thread_calls) == 1
    assert len(rows(db, "messages")) == 3


def test_a_different_reply_from_the_same_lead_is_a_new_event(
    client: TestClient, db: FakeDb
) -> None:
    post(client, EVENT)
    post(client, {**EVENT, "time_replied": "2026-10-22T09:00:00Z"})
    assert len(rows(db, "raw_events")) == 2


def test_storing_failure_is_a_500_so_smartlead_retries(
    client: TestClient,
    db: FakeDb,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"database down {MARKER}")

    monkeypatch.setattr(webhook_module, "insert_raw_event", boom)
    response = post(client, EVENT)
    assert response.status_code == 500 and MARKER not in response.text
    assert MARKER not in caplog.text and "RuntimeError" in caplog.text


# ------------------------------------------------------------------ dedupe key


def test_event_key_uses_the_documented_fields() -> None:
    key, fields = event_key(EVENT)
    assert fields == ["event_type", "campaign_id", "to_email", "time_replied"]
    assert len(key) == 64


def test_event_key_prefers_message_and_lead_ids_when_present() -> None:
    rich = {
        **EVENT,
        "sl_email_lead_id": 555,
        "stats_id": "s-1",
        "reply_message": {"message_id": "<m@x>"},
    }
    key, fields = event_key(rich)
    assert fields == [
        "event_type",
        "campaign_id",
        "sl_email_lead_id",
        "reply_message.message_id",
        "time_replied",
    ]
    assert key != event_key(EVENT)[0]


def test_event_key_ignores_unrelated_fields_and_email_case() -> None:
    base, _ = event_key(EVENT)
    assert event_key({**EVENT, "webhook_id": 9, "subject": "other", "secret_key": "s"})[0] == base
    assert event_key({**EVENT, "to_email": "JANE@Example.com "})[0] == base


def test_event_key_changes_with_the_event() -> None:
    base, _ = event_key(EVENT)
    assert event_key({**EVENT, "time_replied": "2026-10-21T00:00:00Z"})[0] != base
    assert event_key({**EVENT, "campaign_id": 102})[0] != base
    assert event_key({**EVENT, "to_email": "bob@example.com"})[0] != base


def test_event_key_falls_back_to_the_whole_payload() -> None:
    thin = {"event_type": "EMAIL_REPLY", "campaign_id": 101, "to_email": "a@b.com"}
    key, fields = event_key(thin)
    assert fields == ["whole payload"] and len(key) == 64
    assert event_key({**thin, "extra": 1})[0] != key


# ------------------------------------------------------------------ processing


def run(
    db: FakeDb, smartlead: FakeSmartlead, payload: dict[str, Any] = EVENT, event_id: str = "e1"
):
    db.tables.setdefault("raw_events", []).append({"id": event_id, "processed": False})
    return process_reply_event(IngestDeps(db, smartlead, lambda: NOW), event_id, payload)


def test_first_reply_stores_thread_and_messages(db: FakeDb, smartlead: FakeSmartlead) -> None:
    outcome = run(db, smartlead)
    assert outcome.status == FIRST_REPLY and outcome.thread_id
    thread = rows(db, "threads")[0]
    assert (thread["email_lead_id"], thread["email_campaign_id"]) == (555, 101)
    assert thread["lead_email"] == "jane@example.com"
    assert (
        thread["campaign_name"] == "GMC - Example Campaign" and thread["lead_first_name"] == "Jane"
    )
    assert thread["smartlead_category_id"] == 1 and thread["last_reply_at"].startswith("2026-10-20")
    assert [m["direction"] for m in rows(db, "messages")] == ["SENT", "REPLY", "SENT"]
    assert rows(db, "raw_events")[0]["processed"] is True
    assert smartlead.lookups == [(101, "jane@example.com")]  # the event named no lead id


def test_an_event_that_names_the_lead_skips_the_inbox_search(
    db: FakeDb, smartlead: FakeSmartlead
) -> None:
    outcome = run(db, smartlead, {**EVENT, "sl_email_lead_id": "555"})
    assert outcome.status == FIRST_REPLY and smartlead.lookups == []
    assert smartlead.thread_calls[0].lead_id == 555
    assert rows(db, "threads")[0]["lead_email"] == "jane@example.com"


def test_a_second_reply_is_stored_but_not_first(db: FakeDb, smartlead: FakeSmartlead) -> None:
    assert run(db, smartlead).status == FIRST_REPLY
    two_replies = copy.deepcopy(HISTORY)
    second = copy.deepcopy(HISTORY["history"][1])
    second.update(time="2026-10-22T09:00:00.000Z", message_id="<reply-2@example.com>")
    second["email_body"] = "Thanks, one more question."
    two_replies["history"].append(second)
    smartlead.history = two_replies
    later = {**EVENT, "time_replied": "2026-10-22T09:00:00Z"}
    outcome = run(db, smartlead, later, event_id="e2")
    assert outcome.status == FOLLOW_UP
    replies = [m for m in rows(db, "messages") if m["direction"] == "REPLY"]
    assert len(replies) == 2 and len(rows(db, "messages")) == 4  # nothing duplicated
    assert len(rows(db, "threads")) == 1 and "classifications" not in db.tables
    assert rows(db, "raw_events")[1]["processed"] is True


def test_two_replies_arriving_before_the_first_event_is_processed_still_count_as_first(
    db: FakeDb, smartlead: FakeSmartlead
) -> None:
    two_replies = copy.deepcopy(HISTORY)
    second = copy.deepcopy(HISTORY["history"][1])
    second.update(time="2026-10-22T09:00:00.000Z", message_id="<reply-2@example.com>")
    two_replies["history"].append(second)
    smartlead.history = two_replies
    assert run(db, smartlead).status == FIRST_REPLY
    assert run(db, smartlead, event_id="e2").status == FOLLOW_UP


def test_redelivering_processing_does_not_duplicate_messages(
    db: FakeDb, smartlead: FakeSmartlead
) -> None:
    run(db, smartlead)
    run(db, smartlead, event_id="e2")
    assert len(rows(db, "messages")) == 3 and len(rows(db, "threads")) == 1


def test_parse_failure_is_stored_on_the_event_and_nothing_else(
    db: FakeDb, smartlead: FakeSmartlead
) -> None:
    broken = copy.deepcopy(HISTORY)
    broken["history"][1]["type"] = "FORWARD"
    smartlead.history = broken
    outcome = run(db, smartlead)
    assert outcome.status == FAILED
    event = rows(db, "raw_events")[0]
    assert event["processed"] is True and "thread parse failure" in event["process_error"]
    assert "FORWARD" in event["process_error"]
    assert "threads" not in db.tables or rows(db, "threads") == []
    assert rows(db, "messages") == []


def test_parse_failure_through_the_webhook_sends_nothing_anywhere(
    client: TestClient, db: FakeDb, smartlead: FakeSmartlead
) -> None:
    smartlead.history = {"history": [{"type": "REPLY"}]}
    assert post(client, EVENT).status_code == 200  # Smartlead is still answered 200
    event = rows(db, "raw_events")[0]
    assert event["processed"] is True and "parse failure" in event["process_error"]
    assert set(db.tables) == {"raw_events"}  # no thread, message, card or action rows


def test_a_fetch_error_leaves_the_event_unprocessed_for_a_retry(
    db: FakeDb, smartlead: FakeSmartlead
) -> None:
    smartlead.error = SmartleadError("HTTP 500")
    assert run(db, smartlead).status == FAILED
    event = rows(db, "raw_events")[0]
    assert event["processed"] is False and "could not fetch the thread" in event["process_error"]
    assert "threads" not in db.tables


def test_a_lead_missing_from_the_inbox_is_retryable(db: FakeDb) -> None:
    smartlead = FakeSmartlead(item=None)
    assert run(db, smartlead).status == FAILED
    event = rows(db, "raw_events")[0]
    assert event["processed"] is False and "not found" in event["process_error"]


def test_a_thread_of_another_lead_is_refused(db: FakeDb, smartlead: FakeSmartlead) -> None:
    other = {**EVENT, "sl_email_lead_id": 999, "to_email": "someone.else@example.com"}
    assert run(db, smartlead, other).status == FAILED
    event = rows(db, "raw_events")[0]
    assert "does not belong" in event["process_error"] and event["processed"] is False
    assert "threads" not in db.tables


def test_a_thread_with_no_reply_yet_is_retryable(db: FakeDb, smartlead: FakeSmartlead) -> None:
    only_sent = copy.deepcopy(HISTORY)
    only_sent["history"] = [e for e in only_sent["history"] if e["type"] == "SENT"]
    smartlead.history = only_sent
    assert run(db, smartlead).status == FAILED
    assert rows(db, "raw_events")[0]["processed"] is False


def test_an_event_with_no_way_to_find_the_lead_fails_for_good(
    db: FakeDb, smartlead: FakeSmartlead
) -> None:
    thin = {"event_type": "EMAIL_REPLY", "campaign_id": 101}
    assert run(db, smartlead, thin).status == FAILED
    event = rows(db, "raw_events")[0]
    assert event["processed"] is True and "invalid event" in event["process_error"]


def test_a_crash_in_the_background_task_is_recorded(
    client: TestClient, db: FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(MARKER)

    monkeypatch.setattr(webhook_module, "process_reply_event", boom)
    assert post(client, EVENT).status_code == 200
    event = rows(db, "raw_events")[0]
    assert not event.get("processed")
    assert event["process_error"] == "processing crashed: RuntimeError"


# ------------------------------------------------------------------ guard rails


def test_ingest_only_reads_from_smartlead_and_never_touches_slack_or_the_classifier() -> None:
    forbidden = (
        "send_reply",
        "unsubscribe_lead",
        "pause_lead",
        "app.slack",
        "app.classify",
        "anthropic",
        "set_category",
    )
    for module in (webhook_module, process_module, deps_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for word in forbidden:
            assert word not in source, f"{module.__name__} mentions {word}"


# ------------------------------------------------------------------ settings and client


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GMC_CAMPAIGN_IDS", raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize("secret", ["", "   ", "short", "x" * 31, " " + "x" * 30 + " "])
def test_a_blank_or_short_webhook_secret_stops_startup(
    env: pytest.MonkeyPatch, secret: str
) -> None:
    env.setenv("WEBHOOK_SECRET", secret)
    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert "WEBHOOK_SECRET" in str(exc.value)
    if secret.strip():
        assert "needs at least 32 characters" in str(exc.value)
    else:
        assert "WEBHOOK_SECRET: blank" in str(exc.value)


def test_a_32_character_webhook_secret_is_fine(env: pytest.MonkeyPatch) -> None:
    env.setenv("WEBHOOK_SECRET", "x" * 32)
    assert get_settings().WEBHOOK_SECRET == "x" * 32


def test_campaign_ids_come_from_settings(env: pytest.MonkeyPatch) -> None:
    assert get_settings().gmc_campaign_ids == frozenset()
    get_settings.cache_clear()
    env.setenv("GMC_CAMPAIGN_IDS", "101, 102,103,")
    assert get_settings().gmc_campaign_ids == {101, 102, 103}


@pytest.mark.parametrize("bad", ["abc", "101;102", "101,x", "-5"])
def test_bad_campaign_ids_stop_startup(env: pytest.MonkeyPatch, bad: str) -> None:
    env.setenv("GMC_CAMPAIGN_IDS", bad)
    with pytest.raises(ConfigError, match="GMC_CAMPAIGN_IDS"):
        get_settings()
    with pytest.raises(ValueError):
        parse_campaign_ids(bad)


def test_find_inbox_item_pages_until_it_finds_the_lead() -> None:
    import httpx

    from app.smartlead.client import MAX_LIMIT, SmartleadClient

    other = {"lead_email": "other@example.com", "email_lead_id": "1"}
    wanted = {"lead_email": "Jane@Example.com", "email_lead_id": "555"}
    pages = [
        httpx.Response(200, json={"ok": True, "data": [other] * MAX_LIMIT}),
        httpx.Response(200, json={"ok": True, "data": [other, wanted]}),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return pages[len(seen) - 1]

    client = SmartleadClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert client.find_inbox_item(101, "jane@example.com") == wanted
    bodies = [json.loads(r.content) for r in seen]
    assert [b["offset"] for b in bodies] == [0, MAX_LIMIT]
    assert all(r.method == "POST" and r.url.path.endswith("/inbox-replies") for r in seen)


def test_find_inbox_item_returns_none_and_rejects_odd_responses() -> None:
    import httpx

    from app.smartlead.client import SmartleadClient

    def make(body: Any) -> SmartleadClient:
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
        return SmartleadClient("k", transport=transport, sleep=lambda s: None)

    assert make({"ok": True, "data": []}).find_inbox_item(101, "a@b.com") is None
    with pytest.raises(SmartleadError, match="unexpected"):
        make({"ok": True}).find_inbox_item(101, "a@b.com")
