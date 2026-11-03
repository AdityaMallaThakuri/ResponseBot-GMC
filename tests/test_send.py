"""C4: sending an approved draft, behind every lock. A fake Smartlead; nothing real is sent."""

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config.settings import ConfigError, get_settings, parse_allowlist
from app.send.sender import (
    SendConfig,
    SendService,
    mask_email,
    to_email_html,
)
from app.slack.card import RETRY_SEND, with_send_outcome
from app.slack.handlers import handle_retry_send, handle_service_click
from app.slack.service import CardService
from app.smartlead.client import SmartleadClient
from app.smartlead.errors import RecipientNotAllowed, SendDisabledError, SmartleadError
from app.smartlead.models import Lead, Thread
from tests.conftest import REQUIRED as GOOD_VALUES
from tests.fakedb import FakeDb, FakeQuery
from tests.test_draft_flow import USER, FakeSlackApi, card_ids, click_body, make_deps, run
from tests.test_webhook import NOW, rows

MARKER = "SYNTHETICMARKERTEXT"
LEAD = "jane@example.com"


class RecordingSmartlead:
    """Stands in for the Smartlead client's send_reply. Records calls; can fail."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[Thread, str]] = []

    def send_reply(self, thread: Thread, body: str) -> dict[str, bool]:
        if self.error:
            raise self.error
        self.sent.append((thread, body))
        return {"ok": True}


def setup(
    label: str = "info_request",
    *,
    enabled: bool = True,
    allowlist: frozenset[str] = frozenset({LEAD}),
    live: bool = False,
    campaigns: frozenset[int] = frozenset({101}),
    smartlead: RecordingSmartlead | None = None,
    playbook=None,
):
    """A processed thread with a draft card, and a CardService that can send."""
    deps = make_deps(label, playbook=playbook)
    run(deps)
    smartlead = smartlead or RecordingSmartlead()
    config = SendConfig(enabled, allowlist, live, campaigns)
    sender = SendService(
        lambda: deps.db,
        lambda: smartlead,
        deps.drafter.playbook,
        deps.drafter.facts,
        config,
        lambda: NOW,
    )
    service = CardService(lambda: deps.db, deps.drafter.playbook, deps.drafter.facts, sender)
    return deps, service, sender, smartlead


def approve(deps, service):
    channel, ts = card_ids(deps)
    return service.click(channel, ts, "reply_approve", USER)


def draft_row(deps) -> dict[str, Any]:
    return rows(deps.db, "drafts")[0]


def send_actions(deps) -> list[dict[str, Any]]:
    return [a for a in rows(deps.db, "actions") if a["type"] == "send"]


def results(deps) -> list[str]:
    return [a["details"]["result"] for a in send_actions(deps)]


# ------------------------------------------------------------------ the happy path


def test_approving_sends_once_to_the_right_lead_and_records_it() -> None:
    deps, service, _, smartlead = setup()
    result = approve(deps, service)
    assert result.kind == "recorded" and result.send.status == "sent"
    ((thread, body),) = smartlead.sent
    assert (thread.lead.campaign_id, thread.lead.lead_id, thread.to_email) == (101, 555, LEAD)
    assert thread.reply_to_stats_id == "11111111-aaaa-4aaa-8aaa-000000000001"  # the lead's reply
    assert body.startswith("<p>SYNTHETIC T2: Hello Jane.") and "<br>" not in body.replace("<p>", "")
    (action,) = send_actions(deps)
    assert action["triggered_by"] == f"human:{USER}" and action["reversible"] is False
    assert (
        action["details"]["result"] == "sent" and action["details"]["recipient"] == "j***@e***.com"
    )
    assert (
        draft_row(deps)["status"] == "sent"
        and rows(deps.db, "slack_cards")[0]["status"] == "resolved"
    )
    assert result.send.text == "Sent to j***@e***.com"


def test_the_email_body_is_never_stored_in_the_action() -> None:
    deps, service, _, _ = setup()
    approve(deps, service)
    stored = json.dumps(rows(deps.db, "actions"))
    assert "SYNTHETIC T2" not in stored and "Hello Jane" not in stored


def test_saving_an_edit_does_not_send() -> None:
    deps, service, _, smartlead = setup()
    channel, ts = card_ids(deps)
    service.submit_edit(channel, ts, "A new text with no numbers")
    assert smartlead.sent == [] and not send_actions(deps) and draft_row(deps)["status"] == "edited"


def test_an_approved_edit_is_what_gets_sent() -> None:
    deps, service, _, smartlead = setup()
    channel, ts = card_ids(deps)
    service.submit_edit(channel, ts, "My own words.\n\nSecond paragraph & more")
    assert service.click(channel, ts, "reply_approve", USER).send.status == "sent"
    assert smartlead.sent[0][1] == "<p>My own words.</p><p>Second paragraph &amp; more</p>"
    assert rows(deps.db, "decisions")[0]["decision"] == "edited"


def test_the_html_conversion_escapes_and_keeps_line_breaks() -> None:
    assert to_email_html("a < b\nsecond line\n\nnext <b>para</b>") == (
        "<p>a &lt; b<br>second line</p><p>next &lt;b&gt;para&lt;/b&gt;</p>"
    )
    assert mask_email("jane@example.com") == "j***@e***.com"


# ------------------------------------------------------------------ the locks


def test_send_enabled_false_blocks_everything() -> None:
    deps, service, _, smartlead = setup(enabled=False, live=True)
    result = approve(deps, service)
    assert result.send.code == "blocked_send_disabled" and result.send.status == "blocked"
    assert smartlead.sent == [] and results(deps) == ["blocked_send_disabled"]
    assert draft_row(deps)["status"] == "approved"  # still approved, never sent
    assert "SEND_ENABLED is false" in result.send.text


def test_the_allowlist_blocks_a_stranger() -> None:
    deps, service, _, smartlead = setup(allowlist=frozenset({"me@example.org"}))
    result = approve(deps, service)
    assert result.send.code == "blocked_not_allowlisted"
    assert smartlead.sent == [] and results(deps) == ["blocked_not_allowlisted"]
    assert send_actions(deps)[0]["details"]["recipient"] == "j***@e***.com"
    assert draft_row(deps)["status"] == "approved" and result.send.retryable
    assert "SEND_ALLOWLIST" in result.send.text


def test_an_empty_allowlist_blocks_everyone() -> None:
    deps, service, _, smartlead = setup(allowlist=frozenset())
    assert approve(deps, service).send.code == "blocked_not_allowlisted" and smartlead.sent == []


def test_live_send_all_bypasses_the_allowlist() -> None:
    deps, service, _, smartlead = setup(allowlist=frozenset(), live=True)
    assert approve(deps, service).send.status == "sent" and len(smartlead.sent) == 1


def test_the_allowlist_comparison_ignores_case() -> None:
    deps, service, _, smartlead = setup(allowlist=frozenset({LEAD}))
    deps.db.tables["threads"][0]["lead_email"] = "Jane@Example.com"
    deps.db.tables["messages"][1]["from_address"] = "JANE@example.com"
    assert approve(deps, service).send.status == "sent" and smartlead.sent[0][0].to_email == LEAD


def test_the_client_refuses_a_stranger_even_if_the_service_did_not() -> None:
    smartlead = RecordingSmartlead(error=RecipientNotAllowed("not allowlisted"))
    deps, service, _, _ = setup(smartlead=smartlead)
    result = approve(deps, service)
    assert result.send.code == "blocked_not_allowlisted" and draft_row(deps)["status"] == "approved"
    smartlead.error = SendDisabledError("off")
    channel, ts = card_ids(deps)
    assert service.retry_send(channel, ts, USER).send.code == "blocked_send_disabled"


# ------------------------------------------------------------------ safety checks


def force_approved(deps, by: str = USER, decision: str = "approved") -> None:
    """Make the draft approved in the database without going through the Slack click."""
    draft = draft_row(deps)
    draft["status"] = "approved"
    deps.db.tables["decisions"] = [
        {"draft_id": draft["id"], "decided_by": by, "decision": decision}
    ]


def test_a_draft_nobody_approved_is_never_sent() -> None:
    deps, _, sender, smartlead = setup()
    assert sender.send_draft(draft_row(deps)["id"], "human:" + USER).code == "blocked_not_approved"
    draft_row(deps)["status"] = "approved"  # approved status but no decisions row
    assert sender.send_draft(draft_row(deps)["id"], "bot").code == "blocked_not_approved"
    assert smartlead.sent == []


@pytest.mark.parametrize(
    ("by", "decision"),
    [("bot", "approved"), ("system", "edited"), (USER, "skipped"), ("", "approved")],
)
def test_a_decision_by_the_bot_or_a_skip_does_not_count(by: str, decision: str) -> None:
    deps, _, sender, smartlead = setup()
    force_approved(deps, by, decision)
    assert sender.send_draft(draft_row(deps)["id"], "bot").code == "blocked_not_approved"
    assert smartlead.sent == []


def test_a_remaining_placeholder_blocks_the_send() -> None:
    deps, _, sender, smartlead = setup("meeting_request")
    force_approved(deps)
    assert "[TIME OPTIONS]" in draft_row(deps)["text"]
    assert sender.send_draft(draft_row(deps)["id"], "human:" + USER).code == "blocked_placeholder"
    assert smartlead.sent == [] and results(deps) == ["blocked_placeholder"]


def test_an_edit_that_fills_the_placeholder_can_then_be_sent() -> None:
    deps, service, _, smartlead = setup("meeting_request")
    channel, ts = card_ids(deps)
    text = draft_row(deps)["text"].replace("[TIME OPTIONS]", "Tuesday 10am or Wednesday 2pm")
    service.submit_edit(channel, ts, text)
    assert service.click(channel, ts, "reply_approve", USER).send.status == "sent"
    assert len(smartlead.sent) == 1


@pytest.mark.parametrize(
    "extra", [" We closed 17 deals.", " See https://evil.example/x", " Pay $500 a month."]
)
def test_an_edit_with_an_unapproved_claim_can_be_approved_but_not_sent(extra: str) -> None:
    deps, service, _, smartlead = setup()
    channel, ts = card_ids(deps)
    service.submit_edit(channel, ts, f"A plain reply.{extra}")  # a warning, never a block
    result = service.click(channel, ts, "reply_approve", USER)
    assert result.kind == "recorded" and result.send.code == "blocked_claims"
    assert smartlead.sent == [] and draft_row(deps)["status"] == "approved"
    assert "link, price or number" in result.send.text and result.send.retryable


def test_a_draft_that_failed_validation_is_never_sent() -> None:
    deps, _, sender, smartlead = setup()
    force_approved(deps)
    draft_row(deps)["validator_ok"] = False
    assert sender.send_draft(draft_row(deps)["id"], "bot").code == "blocked_claims"
    assert smartlead.sent == []


def test_a_draft_whose_template_left_the_playbook_is_not_sent() -> None:
    deps, _, sender, smartlead = setup()
    force_approved(deps)
    sender.playbook = type(sender.playbook)(sender.playbook.version, {})
    assert sender.send_draft(draft_row(deps)["id"], "bot").code == "blocked_template_missing"
    assert smartlead.sent == []


def test_the_reply_must_be_addressed_to_the_lead_who_wrote() -> None:
    deps, service, _, smartlead = setup()
    deps.db.tables["threads"][0]["lead_email"] = "someone.else@example.com"
    assert approve(deps, service).send.code == "blocked_wrong_recipient"
    assert smartlead.sent == []


def test_a_thread_with_no_lead_reply_cannot_be_answered() -> None:
    deps, service, sender, smartlead = setup()
    force_approved(deps)
    deps.db.tables["messages"] = [m for m in deps.db.tables["messages"] if m["direction"] == "SENT"]
    assert sender.send_draft(draft_row(deps)["id"], "bot").code == "blocked_wrong_recipient"
    assert smartlead.sent == []


def test_only_our_campaigns_are_sent_to() -> None:
    deps, service, _, smartlead = setup(campaigns=frozenset({999}))
    result = approve(deps, service)
    assert result.send.code == "blocked_wrong_campaign" and smartlead.sent == []
    assert send_actions(deps)[0]["details"]["campaign_id"] == 101


def test_a_reply_with_no_stats_id_cannot_be_answered() -> None:
    deps, service, _, smartlead = setup()
    deps.db.tables["messages"][1]["stats_id"] = ""
    assert approve(deps, service).send.code == "blocked_no_stats_id" and smartlead.sent == []


def test_the_newest_reply_is_the_one_we_answer() -> None:
    deps, service, _, smartlead = setup()
    newer = dict(deps.db.tables["messages"][1], id="m-new", sent_at="2026-10-22T09:00:00+00:00")
    newer["stats_id"] = "99999999-newer"
    deps.db.tables["messages"].append(newer)
    approve(deps, service)
    assert smartlead.sent[0][0].reply_to_stats_id == "99999999-newer"


# ------------------------------------------------------------------ once only


def test_a_second_click_cannot_send_again() -> None:
    deps, service, _, smartlead = setup()
    channel, ts = card_ids(deps)
    assert service.click(channel, ts, "reply_approve", USER).send.status == "sent"
    assert service.click(channel, ts, "reply_approve", USER).kind == "ignored"
    assert service.retry_send(channel, ts, USER).kind == "ignored"
    assert len(smartlead.sent) == 1 and results(deps) == ["sent"]


def test_asking_the_sender_twice_sends_once() -> None:
    deps, service, sender, smartlead = setup()
    approve(deps, service)
    again = sender.send_draft(draft_row(deps)["id"], "human:" + USER)
    assert again.code == "blocked_already_sent" and not again.retryable
    assert len(smartlead.sent) == 1 and results(deps) == ["sent", "blocked_already_sent"]


class LockedQuery(FakeQuery):
    lock = threading.Lock()

    def execute(self):
        with self.lock:  # one database statement at a time, as in Postgres
            return super().execute()


class LockedDb(FakeDb):
    def table(self, name: str) -> LockedQuery:
        return LockedQuery(self, name)


def test_simultaneous_sends_still_send_once() -> None:
    deps, service, sender, smartlead = setup()
    locked = LockedDb()
    locked.tables, locked.calls = deps.db.tables, deps.db.calls
    sender._get_db = lambda: locked  # noqa: SLF001
    force_approved(deps)
    draft_id = draft_row(deps)["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: sender.send_draft(draft_id, "human:" + USER), range(8)))
    assert sum(1 for o in outcomes if o.code == "sent") == 1 and len(smartlead.sent) == 1


def test_claiming_a_draft_is_a_conditional_update() -> None:
    from app.db.client import claim_draft

    deps, _, _, _ = setup()
    draft_id = draft_row(deps)["id"]
    draft_row(deps)["status"] = "approved"
    assert claim_draft(deps.db, draft_id, "approved", "sent") is True
    assert claim_draft(deps.db, draft_id, "approved", "sent") is False


# ------------------------------------------------------------------ errors and retry


def test_a_smartlead_error_is_recorded_without_the_body_and_leaves_the_draft_retryable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    smartlead = RecordingSmartlead(error=SmartleadError(f"HTTP 500: {MARKER} echoed"))
    deps, service, _, _ = setup(smartlead=smartlead)
    channel, ts = card_ids(deps)
    service.submit_edit(channel, ts, f"Plain text {MARKER}")
    result = service.click(channel, ts, "reply_approve", USER)
    assert result.send.status == "failed" and result.send.retryable
    assert result.send.text == "Send failed (SmartleadError)"
    (action,) = send_actions(deps)
    assert action["details"] == {
        "result": "failed",
        "draft_id": draft_row(deps)["id"],
        "error": "SmartleadError",
        "recipient": "j***@e***.com",
    }
    assert (
        draft_row(deps)["status"] == "approved"
        and rows(deps.db, "slack_cards")[0]["status"] == "failed"
    )
    everything = caplog.text + json.dumps(rows(deps.db, "actions")) + result.send.text
    assert MARKER not in everything


def test_an_unexpected_error_is_handled_the_same_way() -> None:
    deps, service, _, _ = setup(smartlead=RecordingSmartlead(error=ConnectionError("boom")))
    result = approve(deps, service)
    assert result.send.code == "failed" and "ConnectionError" in result.send.text
    assert draft_row(deps)["status"] == "approved"


def test_a_failed_send_can_be_retried_and_then_sends_once() -> None:
    smartlead = RecordingSmartlead(error=SmartleadError("HTTP 503"))
    deps, service, _, _ = setup(smartlead=smartlead)
    channel, ts = card_ids(deps)
    assert service.click(channel, ts, "reply_approve", USER).send.code == "failed"
    smartlead.error = None
    retry = service.retry_send(channel, ts, USER)
    assert retry.send.status == "sent" and len(smartlead.sent) == 1
    assert results(deps) == ["failed", "sent"]
    assert (
        draft_row(deps)["status"] == "sent"
        and rows(deps.db, "slack_cards")[0]["status"] == "resolved"
    )
    assert service.retry_send(channel, ts, USER).kind == "ignored"


def test_a_blocked_send_can_be_retried_after_the_cause_is_fixed() -> None:
    deps, service, sender, smartlead = setup(allowlist=frozenset())
    channel, ts = card_ids(deps)
    assert service.click(channel, ts, "reply_approve", USER).send.code == "blocked_not_allowlisted"
    sender.config = SendConfig(True, frozenset({LEAD}), False, frozenset({101}))
    assert service.retry_send(channel, ts, USER).send.status == "sent" and len(smartlead.sent) == 1


def test_retry_needs_an_approved_card_and_a_slack_user() -> None:
    deps, service, _, smartlead = setup()
    channel, ts = card_ids(deps)
    assert service.retry_send(channel, ts, USER).kind == "ignored"  # not approved yet
    assert service.retry_send("C0X", "9.9", USER).kind == "ignored"
    assert smartlead.sent == []


# ------------------------------------------------------------------ the card


def test_with_send_outcome_replaces_buttons_and_offers_retry() -> None:
    blocks = [{"type": "section"}, {"type": "actions", "elements": []}]
    sent = with_send_outcome(blocks, "Sent to j***@e***.com")
    assert [b["type"] for b in sent] == ["section", "context"] and "Sent to" in json.dumps(sent)
    retry = with_send_outcome(sent, "Send failed (SmartleadError)", "1000.1")
    assert [b["type"] for b in retry] == ["section", "context", "actions"]
    assert (
        retry[-1]["elements"][0]["action_id"] == RETRY_SEND
        and retry[-1]["elements"][0]["value"] == "1000.1"
    )
    again = with_send_outcome(retry, "Sent to x", None)  # the old outcome and button are replaced
    assert [b["type"] for b in again] == ["section", "context"] and json.dumps(again).count(
        "Send failed"
    ) == 0


def test_the_approve_click_shows_the_outcome_on_the_card() -> None:
    deps, service, _, _ = setup(allowlist=frozenset())
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(handle_service_click("reply_approve", _ack, click_body(channel, ts), api, service))
    blocks = api.updates[0]["blocks"]
    assert "not in SEND_ALLOWLIST" in json.dumps(blocks)
    assert blocks[-1]["elements"][0]["action_id"] == RETRY_SEND  # a blocked send can be retried


def test_a_successful_send_shows_sent_and_no_retry_button() -> None:
    deps, service, _, _ = setup()
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(handle_service_click("reply_approve", _ack, click_body(channel, ts), api, service))
    blocks = api.updates[0]["blocks"]
    assert "Sent to j***@e***.com" in json.dumps(blocks)
    assert all(b["type"] != "actions" for b in blocks)


def test_the_retry_button_sends_and_updates_the_card() -> None:
    smartlead = RecordingSmartlead(error=SmartleadError("HTTP 503"))
    deps, service, _, _ = setup(smartlead=smartlead)
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(handle_service_click("reply_approve", _ack, click_body(channel, ts), api, service))
    smartlead.error = None
    failed_blocks = api.updates[-1]["blocks"]
    asyncio.run(handle_retry_send(_ack, click_body(channel, ts, failed_blocks), api, service))
    assert (
        "Sent to j***@e***.com" in json.dumps(api.updates[-1]["blocks"])
        and len(smartlead.sent) == 1
    )


async def _ack(**kwargs: Any) -> None:
    return None


# ------------------------------------------------------------------ the Smartlead client's own lock


def client(**kw: Any) -> tuple[SmartleadClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    made = SmartleadClient("k", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw)
    return made, requests


def thread_to(email: str | None) -> Thread:
    return Thread(Lead(101, 555, email), reply_to_stats_id="s1", to_email=email)


def test_the_client_refuses_without_an_allowlist_entry() -> None:
    made, requests = client(send_enabled=True)
    with pytest.raises(RecipientNotAllowed):
        made.send_reply(thread_to(LEAD), "hi")
    made, requests = client(send_enabled=True, send_allowlist=frozenset({"other@example.com"}))
    with pytest.raises(RecipientNotAllowed):
        made.send_reply(thread_to(LEAD), "hi")
    with pytest.raises(RecipientNotAllowed):
        made.send_reply(thread_to(None), "hi")
    assert requests == []  # refused before any HTTP call


def test_the_client_sends_to_an_allowlisted_address_in_any_case() -> None:
    made, requests = client(send_enabled=True, send_allowlist=frozenset({"Jane@Example.com"}))
    made.send_reply(thread_to("JANE@example.com"), "hi")
    assert len(requests) == 1 and requests[0].url.path.endswith("/campaigns/101/reply-email-thread")


def test_live_send_all_lets_the_client_send_to_anyone_but_sending_must_still_be_on() -> None:
    made, requests = client(send_enabled=True, live_send_all=True)
    made.send_reply(thread_to("anyone@example.com"), "hi")
    assert len(requests) == 1
    off, none = client(send_enabled=False, live_send_all=True)
    with pytest.raises(SendDisabledError):
        off.send_reply(thread_to(LEAD), "hi")
    assert none == []


# ------------------------------------------------------------------ settings


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.chdir(tmp_path)
    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    for name in ("SEND_ENABLED", "SEND_ALLOWLIST", "LIVE_SEND_ALL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_sending_is_off_and_nobody_is_allowed_by_default(env: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    assert settings.SEND_ENABLED is False and settings.LIVE_SEND_ALL is False
    assert settings.send_allowlist == frozenset()
    config = SendConfig.from_settings(settings)
    assert (config.send_enabled, config.allowlist, config.live_send_all) == (
        False,
        frozenset(),
        False,
    )


def test_the_allowlist_is_parsed_lower_cased_and_validated(env: pytest.MonkeyPatch) -> None:
    env.setenv("SEND_ALLOWLIST", " Me@Example.com, you@example.org ,")
    assert get_settings().send_allowlist == {"me@example.com", "you@example.org"}
    assert parse_allowlist("") == frozenset()
    for bad in ("not-an-email", "a@b", "a@b.com b@c.com", "a@b.com;c@d.com"):
        with pytest.raises(ValueError):
            parse_allowlist(bad)
    get_settings.cache_clear()
    env.setenv("SEND_ALLOWLIST", "oops")
    with pytest.raises(ConfigError, match="SEND_ALLOWLIST"):
        get_settings()


def test_the_smartlead_client_is_built_with_both_locks_from_settings(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("SEND_ENABLED", "true")
    env.setenv("SEND_ALLOWLIST", LEAD)
    client_ = SmartleadClient.from_settings(get_settings())
    assert client_._send_enabled is True and client_._send_allowlist == {LEAD}  # noqa: SLF001
    assert client_._live_send_all is False  # noqa: SLF001
