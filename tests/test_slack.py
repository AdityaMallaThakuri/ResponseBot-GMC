import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from slack_sdk.errors import SlackApiError
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.web.async_slack_response import AsyncSlackResponse

from app.config.settings import Settings, get_settings
from app.ingest.deps import IngestDeps
from app.main import app
from app.slack.card import (
    APPROVE,
    EDIT,
    SKIP,
    ThreadSummary,
    build_card_blocks,
    escape_mrkdwn,
    fallback_text,
    post_test_card,
    resolved_blocks,
)
from app.slack.handlers import handle_card_click
from tests.fakedb import FakeDb

ROOT = Path(__file__).resolve().parent.parent
SECRET = "signing-secret-for-tests"
TOKEN = "xoxb-test-token-1234"
TS = "1700000000.000100"
EXCERPT = "Please send some times - zebra-quartz-unique"

SUMMARY = ThreadSummary(
    thread_ref="thread-123",
    lead_name="Jane Doe",
    company="Acme & Co",
    campaign="GMC - Wave 1",
    reply_excerpt=EXCERPT,
    label="meeting_request",
)


def click_payload(action_id: str = APPROVE, user_id: str = "U012ABCDEF") -> dict:
    blocks = build_card_blocks(SUMMARY)
    return {
        "type": "block_actions",
        "user": {"id": user_id, "username": "ray", "team_id": "T012345"},
        "api_app_id": "A012345",
        "team": {"id": "T012345", "domain": "example"},
        "channel": {"id": "C012345", "name": "reply-bot-test"},
        "container": {"type": "message", "message_ts": TS, "channel_id": "C012345"},
        "message": {"type": "message", "ts": TS, "blocks": blocks},
        "response_url": "https://hooks.slack.com/actions/T012345/1/abc",
        "trigger_id": "13345224609.738474920.8088930838d88f008e0",
        "actions": [
            {
                "action_id": action_id,
                "block_id": "reply_card_actions",
                "type": "button",
                "value": "thread-123",
                "action_ts": "1700000001.000200",
            }
        ],
    }


# -- card -------------------------------------------------------------------------


def test_card_has_three_buttons_carrying_the_thread_ref() -> None:
    blocks = build_card_blocks(SUMMARY)
    (actions,) = [b for b in blocks if b["type"] == "actions"]
    assert [e["action_id"] for e in actions["elements"]] == [APPROVE, EDIT, SKIP]
    assert [e["text"]["text"] for e in actions["elements"]] == ["Approve", "Edit", "Skip"]
    assert {e["value"] for e in actions["elements"]} == {"thread-123"}
    assert actions["elements"][0]["style"] == "primary"


def test_card_escapes_and_truncates_user_text() -> None:
    summary = ThreadSummary("t", "A <b>", "C & D", "x" * 1000, company=None)
    text = json.dumps(build_card_blocks(summary))
    assert "<b>" not in text and "A &lt;b&gt;" in text and "C &amp; D" in text
    quote = [b for b in build_card_blocks(summary) if b["type"] == "section"][1]
    assert len(quote["text"]["text"]) <= 302 and quote["text"]["text"].endswith("…")
    assert escape_mrkdwn("<@U1> & <!channel>") == "&lt;@U1&gt; &amp; &lt;!channel&gt;"


def test_fallback_text_has_no_email_body() -> None:
    assert EXCERPT not in fallback_text(SUMMARY)


@pytest.mark.parametrize(
    ("action_id", "words"),
    [(APPROVE, "approved"), (EDIT, "edit"), (SKIP, "skipped")],
)
def test_resolved_blocks_drop_buttons_and_name_the_clicker(action_id: str, words: str) -> None:
    out = resolved_blocks(build_card_blocks(SUMMARY), "U012ABCDEF", action_id)
    assert not [b for b in out if b["type"] == "actions"]
    assert out[:3] == build_card_blocks(SUMMARY)[:3]
    last = out[-1]["elements"][0]["text"]
    assert "<@U012ABCDEF>" in last and words in last


def test_resolved_blocks_reject_unknown_action() -> None:
    with pytest.raises(ValueError):
        resolved_blocks([], "U1", "reply_delete")


def test_post_test_card_posts_blocks_and_returns_ts() -> None:
    client = MagicMock()
    client.chat_postMessage.return_value = {"ok": True, "ts": "123.456"}
    assert post_test_card("#reply-bot-test", SUMMARY, client=client) == "123.456"
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "#reply-bot-test"
    assert kwargs["blocks"] == build_card_blocks(SUMMARY)
    assert EXCERPT not in kwargs["text"]


def test_post_test_card_builds_its_client_from_the_bot_token(env: None) -> None:
    with patch("app.slack.card.WebClient") as web_client:
        web_client.return_value.chat_postMessage.return_value = {"ts": "1.2"}
        post_test_card("C1", SUMMARY)
    assert web_client.call_args.kwargs == {"token": "test"}  # SLACK_BOT_TOKEN from the env fixture


# -- button handlers, called directly with a fake payload ----------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize(
    ("action_id", "words"),
    [(APPROVE, "approved"), (EDIT, "edit"), (SKIP, "skipped")],
)
@pytest.mark.anyio
async def test_click_acks_then_updates_the_message(action_id: str, words: str) -> None:
    ack, client = AsyncMock(), MagicMock(chat_update=AsyncMock())
    await handle_card_click(action_id, ack, click_payload(action_id, "U0AAAAAAA"), client)
    ack.assert_awaited_once()
    kwargs = client.chat_update.await_args.kwargs
    assert (kwargs["channel"], kwargs["ts"]) == ("C012345", TS)
    assert not [b for b in kwargs["blocks"] if b["type"] == "actions"]
    assert "<@U0AAAAAAA>" in kwargs["blocks"][-1]["elements"][0]["text"]
    assert words in kwargs["blocks"][-1]["elements"][0]["text"]


@pytest.mark.anyio
async def test_click_without_a_message_only_acks() -> None:
    ack, client = AsyncMock(), MagicMock(chat_update=AsyncMock())
    body = click_payload()
    del body["message"]
    await handle_card_click(APPROVE, ack, body, client)
    ack.assert_awaited_once()
    client.chat_update.assert_not_awaited()


@pytest.mark.anyio
async def test_update_failure_is_swallowed_and_logged_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ack = AsyncMock()
    error = SlackApiError("boom", {"ok": False, "error": "message_not_found"})
    failing = AsyncMock(side_effect=error)
    with caplog.at_level(logging.DEBUG):
        await handle_card_click(SKIP, ack, click_payload(SKIP), MagicMock(chat_update=failing))
    ack.assert_awaited_once()
    assert "message_not_found" in caplog.text
    assert EXCERPT not in caplog.text


# -- the /slack/events route and signature verification ------------------------------


@pytest.fixture
def slack_api(env: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    get_settings.cache_clear()
    auth = AsyncSlackResponse(
        client=None,
        http_verb="POST",
        api_url="https://slack.com/api/auth.test",
        req_args={},
        data={"ok": True, "user_id": "U0BOT", "bot_id": "B0BOT", "team_id": "T012345"},
        headers={},
        status_code=200,
    )
    update = AsyncMock(return_value={"ok": True})
    app.state.ingest = IngestDeps(db=FakeDb(), smartlead=None)  # an empty database: no cards
    with (
        patch.object(AsyncWebClient, "auth_test", AsyncMock(return_value=auth)),
        patch.object(AsyncWebClient, "chat_update", update),
    ):
        yield update
    del app.state.ingest


def post_click(
    client: TestClient,
    payload: dict,
    *,
    secret: str = SECRET,
    timestamp: int | None = None,
    sign_body: str | None = None,
    drop: tuple[str, ...] = (),
):
    body = urlencode({"payload": json.dumps(payload)})
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signature = SignatureVerifier(secret).generate_signature(
        timestamp=ts, body=sign_body if sign_body is not None else body
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": signature,
    }
    for name in drop:
        headers.pop(name)
    return client.post("/slack/events", content=body, headers=headers)


def test_valid_signature_reaches_the_feedback_handler(slack_api: AsyncMock) -> None:
    db = app.state.ingest.db
    db.tables["drafts"] = [{"id": "d1", "text": "x"}]
    db.tables["slack_cards"] = [
        {"id": "c1", "channel": "C012345", "slack_ts": TS, "draft_id": "d1", "status": "posted"}
    ]
    with TestClient(app) as client:
        response = post_click(client, click_payload("draft_good"))
    assert response.status_code == 200
    slack_api.assert_awaited_once()
    kwargs = slack_api.await_args.kwargs
    assert (kwargs["channel"], kwargs["ts"]) == ("C012345", TS)
    assert "<@U012ABCDEF> marked this a good draft" in kwargs["blocks"][-1]["elements"][0]["text"]
    assert db.tables["drafts"][0]["feedback"] == "good"


def test_the_approve_button_is_no_longer_handled(slack_api: AsyncMock) -> None:
    with TestClient(app) as client:
        response = post_click(client, click_payload(APPROVE))
    assert response.status_code == 404
    slack_api.assert_not_awaited()


@pytest.mark.parametrize(
    "case",
    [
        "wrong_secret",
        "tampered_body",
        "stale_timestamp",
        "future_timestamp",
        "no_signature",
        "no_timestamp",
    ],
)
def test_bad_signatures_are_rejected_before_any_handler_runs(
    slack_api: AsyncMock, case: str
) -> None:
    options = {
        "wrong_secret": {"secret": "not-the-signing-secret"},
        "tampered_body": {"sign_body": "payload=%7B%7D"},
        "stale_timestamp": {"timestamp": int(time.time()) - 3600},
        "future_timestamp": {"timestamp": int(time.time()) + 3600},
        "no_signature": {"drop": ("X-Slack-Signature",)},
        "no_timestamp": {"drop": ("X-Slack-Request-Timestamp",)},
    }[case]
    with TestClient(app) as client:
        response = post_click(client, click_payload(APPROVE), **options)
    assert response.status_code == 401
    slack_api.assert_not_awaited()


def test_unsigned_plain_post_is_rejected(slack_api: AsyncMock) -> None:
    with TestClient(app) as client:
        response = client.post("/slack/events", json={"type": "block_actions"})
    assert response.status_code == 401
    slack_api.assert_not_awaited()


def test_signature_uses_the_configured_secret_not_a_default(slack_api: AsyncMock) -> None:
    # signed with the env-fixture default ("test"), not SLACK_SIGNING_SECRET
    with TestClient(app) as client:
        response = post_click(client, click_payload(SKIP), secret="test")
    assert response.status_code == 401


def test_health_still_works_with_slack_mounted(slack_api: AsyncMock) -> None:
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_secrets_and_email_text_never_reach_the_logs(
    slack_api: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        post_click(client, click_payload(APPROVE))
        post_click(client, click_payload(APPROVE), secret="wrong")
    assert TOKEN not in caplog.text
    assert SECRET not in caplog.text
    assert EXCERPT not in caplog.text


# -- settings and .env.example --------------------------------------------------------

CHANNELS = [
    "SLACK_DRAFT_CHANNEL",
    "SLACK_LOG_CHANNEL",
    "SLACK_ALERT_CHANNEL",
    "SLACK_SHADOW_CHANNEL",
    "SLACK_OPS_CHANNEL",
]


def test_channel_settings_are_optional_and_blank_means_unset(
    env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # not the repo root: its .env may set real channels
    for name in CHANNELS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert all(getattr(settings, name) is None for name in CHANNELS)
    monkeypatch.setenv("SLACK_DRAFT_CHANNEL", "   ")
    monkeypatch.setenv("SLACK_LOG_CHANNEL", "C0LOG")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.SLACK_DRAFT_CHANNEL is None and settings.SLACK_LOG_CHANNEL == "C0LOG"


def test_env_example_lists_every_setting_with_no_values() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = dict(line.split("=", 1) for line in lines if "=" in line)
    assert set(Settings.model_fields) <= set(assignments)
    for name in [*CHANNELS, "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"]:
        assert assignments[name] == ""
