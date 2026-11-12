"""C2: classify the first reply, route it, post a Slack card. Fakes for the model and Slack."""

import asyncio
import copy
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.classify.classifier import Classification, ClassifierError, load_config
from app.classify.routing import (
    LANE_DRAFT,
    LANE_HUMAN_ONLY,
    LANE_WOULD_SUPPRESS,
    NOTICE_ONLY_LABELS,
    route,
)
from app.config.settings import get_settings
from app.ingest.deps import IngestConfig, IngestDeps
from app.ingest.process import FIRST_REPLY, FOLLOW_UP, process_reply_event
from app.main import app
from app.slack.decisions import record_card_click
from app.slack.handlers import handle_card_click
from app.smartlead.errors import SmartleadError
from app.smartlead.thread import parse_thread
from tests.conftest import REQUIRED as GOOD_VALUES
from tests.fakedb import FakeDb
from tests.test_webhook import EVENT, HISTORY, NOW, FakeSmartlead, post, rows

LABELS = list(load_config()["labels"])
DRAFT_LABELS = [
    label
    for label in LABELS
    if label not in NOTICE_ONLY_LABELS | {"do_not_contact", "not_interested"}
]
REPLY_TEXT = next(m.text for m in parse_thread(HISTORY) if m.direction.value == "REPLY")


class FakeClassifier:
    model = "claude-haiku-5-5"
    version = "v-test"

    def __init__(
        self, result: Classification | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or llm("info_request", 0.82)
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def classify(self, reply: str, campaign: str, outreach: str) -> Classification:
        self.calls.append((reply, campaign, outreach))
        if self.error:
            raise self.error
        return self.result


class FakeSlack:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.posts: list[dict[str, Any]] = []

    def chat_postMessage(self, channel: str, text: str, blocks: list) -> dict[str, str]:  # noqa: N802
        if self.fail:
            raise RuntimeError("slack is down")
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})
        return {"ts": f"1000.{len(self.posts)}", "channel": "C0TEST"}


def llm(
    label: str, confidence: float, reason: str = "The lead asks how it works."
) -> Classification:
    return Classification(label, confidence, reason, "llm")


def make_deps(
    db: FakeDb,
    classifier: FakeClassifier | None = None,
    slack: FakeSlack | None = None,
    smartlead: FakeSlack | FakeSmartlead | None = None,
    config: IngestConfig | None = None,
) -> IngestDeps:
    return IngestDeps(
        db=db,
        smartlead=smartlead or FakeSmartlead(),
        clock=lambda: NOW,
        classifier=classifier or FakeClassifier(),
        slack=slack or FakeSlack(),
        config=config or IngestConfig(draft_channel="reply-bot-test"),
    )


def run(deps: IngestDeps, payload: dict[str, Any] = EVENT, event_id: str = "e1"):
    deps.db.tables.setdefault("raw_events", []).append({"id": event_id, "processed": False})
    return process_reply_event(deps, event_id, payload)


def all_text(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


# ------------------------------------------------------------------ routing, every lane


@pytest.mark.parametrize("label", DRAFT_LABELS)
def test_other_labels_go_to_the_draft_lane(label: str) -> None:
    plan = route(llm(label, 0.8), 0.75, 0.90)
    assert (plan.lane, plan.would_suppress, plan.note) == (LANE_DRAFT, False, None)


def test_the_draft_lane_covers_exactly_the_labels_with_a_playbook_reply() -> None:
    assert set(DRAFT_LABELS) == {
        "meeting_request", "info_request", "free_list_request", "price_question",
        "price_and_info", "lead_magnet_request", "trial_request", "booked_confirmation",
        "objection_info", "objection_price",
    }  # fmt: skip


@pytest.mark.parametrize("label", sorted(NOTICE_ONLY_LABELS))
def test_notice_only_labels_never_get_a_draft(label: str) -> None:
    plan = route(llm(label, 0.99), 0.75, 0.90)
    assert (plan.lane, plan.would_suppress) == (LANE_HUMAN_ONLY, False)


def test_out_of_office_and_auto_replies_get_a_short_note() -> None:
    assert "no reply needed" in route(llm("out_of_office", 0.95), 0.75, 0.90).note
    assert "no reply needed" in route(llm("auto_reply_or_bounce", 0.95), 0.75, 0.90).note
    assert route(llm("wrong_person", 0.95), 0.75, 0.90).note is None


def test_do_not_contact_would_suppress_when_confident_enough() -> None:
    for confidence in (0.75, 0.9, 1.0):
        plan = route(llm("do_not_contact", confidence), 0.75, 0.90)
        assert (plan.lane, plan.would_suppress) == (LANE_WOULD_SUPPRESS, True)
    low = route(llm("do_not_contact", 0.7), 0.75, 0.90)
    assert (low.lane, low.would_suppress) == (LANE_HUMAN_ONLY, False)


def test_not_interested_is_human_only_with_a_close_suggestion_from_0_90() -> None:
    assert route(llm("not_interested", 0.95), 0.75, 0.90).note == "suggest: close"
    assert route(llm("not_interested", 0.90), 0.75, 0.90).note == "suggest: close"
    for confidence in (0.89, 0.8, 0.5):
        plan = route(llm("not_interested", confidence), 0.75, 0.90)
        assert (plan.lane, plan.note, plan.would_suppress) == (LANE_HUMAN_ONLY, None, False)


def test_below_the_confidence_threshold_is_human_only_with_no_draft() -> None:
    plan = route(llm("info_request", 0.74), 0.75, 0.90)
    assert plan.lane == LANE_HUMAN_ONLY


def test_a_classifier_error_is_human_only() -> None:
    plan = route(None, 0.75, 0.90)
    assert (plan.lane, plan.reason) == (LANE_HUMAN_ONLY, "classifier error")


def test_only_do_not_contact_ever_records_a_would_suppress() -> None:
    for label in LABELS:
        for confidence in (0.0, 0.5, 0.75, 0.9, 1.0):
            plan = route(llm(label, confidence), 0.75, 0.90)
            assert plan.would_suppress == (label == "do_not_contact" and confidence >= 0.75)


# ------------------------------------------------------------------ end to end through the pipeline


def test_the_first_reply_is_classified_with_the_reply_campaign_and_what_we_sent() -> None:
    db, classifier = FakeDb(), FakeClassifier()
    outcome = run(make_deps(db, classifier))
    assert outcome.status == FIRST_REPLY and outcome.classified
    reply, campaign, outreach = classifier.calls[0]
    messages = parse_thread(HISTORY)
    assert reply == REPLY_TEXT and campaign == "GMC - Example Campaign"
    assert outreach == next(m.text for m in messages if m.direction.value == "SENT")
    row = rows(db, "classifications")[0]
    assert (row["label"], row["confidence"], row["reason"]) == (
        "info_request",
        0.82,
        "The lead asks how it works.",
    )
    assert (row["prompt_version"], row["model"]) == ("v-test", "claude-haiku-5-5")
    thread, reply_msg = (
        rows(db, "threads")[0],
        next(m for m in rows(db, "messages") if m["direction"] == "REPLY"),
    )
    assert row["thread_id"] == thread["id"] and row["reply_message_id"] == reply_msg["id"]
    assert row["smartlead_category_id"] == 1 and "rule_hit" not in row


def test_a_rule_result_is_stored_with_its_rule_name() -> None:
    result = Classification(
        "do_not_contact", 1.0, "rule: asks to unsubscribe", "rule", rule_hit="unsubscribe"
    )
    db = FakeDb()
    run(make_deps(db, FakeClassifier(result)))
    row = rows(db, "classifications")[0]
    assert (row["rule_hit"], row["model"], row["confidence"]) == ("unsubscribe", "rules", 1.0)


def test_do_not_contact_records_would_suppress_and_posts_a_notice_but_calls_nothing() -> None:
    db, slack, smartlead = FakeDb(), FakeSlack(), FakeSmartlead()
    result = Classification(
        "do_not_contact", 1.0, "rule: asks to unsubscribe", "rule", rule_hit="unsubscribe"
    )
    run(make_deps(db, FakeClassifier(result), slack, smartlead))
    (action,) = rows(db, "actions")
    assert (action["type"], action["triggered_by"], action["reversible"]) == (
        "would_suppress",
        "bot",
        False,
    )
    assert (
        action["details"]["label"] == "do_not_contact" and action["details"]["scope"] == "campaign"
    )
    (post_,) = slack.posts
    text = all_text(post_["blocks"])
    assert "DO NOT REPLY" in text and "actions" not in [b["type"] for b in post_["blocks"]]
    assert rows(db, "slack_cards")[0]["lane"] == "auto_action"
    assert len(smartlead.thread_calls) == 1  # only C1's read of the thread


def test_not_interested_posts_a_notice_with_suggest_close_and_no_action() -> None:
    db, slack = FakeDb(), FakeSlack()
    run(make_deps(db, FakeClassifier(llm("not_interested", 0.95, "A polite decline.")), slack))
    text = all_text(slack.posts[0]["blocks"])
    assert (
        "suggest: close" in text
        and "human only" in text
        and "actions" not in [b["type"] for b in slack.posts[0]["blocks"]]
    )
    assert not rows(db, "actions") and not rows(db, "drafts")
    assert rows(db, "slack_cards")[0]["lane"] == "human_only"


def test_not_interested_below_0_90_has_no_close_suggestion() -> None:
    slack = FakeSlack()
    run(make_deps(FakeDb(), FakeClassifier(llm("not_interested", 0.85)), slack))
    assert "suggest: close" not in all_text(slack.posts[0]["blocks"])
    assert "human only" in all_text(slack.posts[0]["blocks"])


@pytest.mark.parametrize(
    "label", ["out_of_office", "wrong_person", "auto_reply_or_bounce", "off_script"]
)
def test_notice_only_labels_post_a_human_only_notice(label: str) -> None:
    db, slack = FakeDb(), FakeSlack()
    run(make_deps(db, FakeClassifier(llm(label, 0.95)), slack))
    blocks = slack.posts[0]["blocks"]
    assert "human only" in all_text(blocks) and "actions" not in [b["type"] for b in blocks]
    assert not rows(db, "actions")


def test_low_confidence_goes_to_a_human_with_no_buttons() -> None:
    slack = FakeSlack()
    run(make_deps(FakeDb(), FakeClassifier(llm("info_request", 0.6)), slack))
    blocks = slack.posts[0]["blocks"]
    assert "human only" in all_text(blocks) and "actions" not in [b["type"] for b in blocks]


def test_a_draft_lane_card_without_a_drafter_has_no_buttons() -> None:
    db, slack = FakeDb(), FakeSlack()
    run(make_deps(db, FakeClassifier(llm("meeting_request", 0.9)), slack))
    blocks = slack.posts[0]["blocks"]
    assert "actions" not in [b["type"] for b in blocks]
    assert rows(db, "slack_cards")[0]["lane"] == "human_only"  # no draft, so no draft card
    assert not rows(db, "drafts")


def test_the_card_shows_lead_campaign_reply_label_confidence_reason_and_lane() -> None:
    db, slack = FakeDb(), FakeSlack()
    run(make_deps(db, FakeClassifier(llm("info_request", 0.82, "Asks how it works.")), slack))
    post_ = slack.posts[0]
    text = all_text(post_["blocks"])
    for expected in (
        "Jane",
        "GMC - Example Campaign",
        REPLY_TEXT,
        "info_request",
        "0.82",
        "Asks how it works.",
        "lane:",
    ):
        assert expected in text
    assert REPLY_TEXT not in post_["text"]  # the notification text has no email body


def test_a_classifier_error_posts_a_human_only_notice_and_stores_no_classification() -> None:
    for error in (ClassifierError("invalid answer after 3 attempts"), RuntimeError("api down")):
        db, slack = FakeDb(), FakeSlack()
        outcome = run(make_deps(db, FakeClassifier(error=error), slack))
        assert outcome.classified and not rows(db, "classifications")
        text = all_text(slack.posts[0]["blocks"])
        assert "classifier error" in text and "human only" in text
        assert "actions" not in [b["type"] for b in slack.posts[0]["blocks"]]
        assert rows(db, "raw_events")[0]["processed"] is True


# ------------------------------------------------------------------ channels


def test_channel_per_lane() -> None:
    cfg = IngestConfig(draft_channel="drafts", alert_channel="alerts")
    assert cfg.channel_for("draft") == "drafts"
    assert cfg.channel_for("human_only") == cfg.channel_for("would_suppress") == "alerts"
    only_draft = IngestConfig(draft_channel="drafts")
    assert only_draft.channel_for("human_only") == "drafts"
    assert IngestConfig().channel_for("draft") is None


def test_cards_go_to_the_channel_for_their_lane_and_the_card_row_keeps_the_channel_id() -> None:
    cfg = IngestConfig(draft_channel="drafts", alert_channel="alerts")
    db, slack = FakeDb(), FakeSlack()
    run(make_deps(db, FakeClassifier(llm("meeting_request", 0.9)), slack, config=cfg))
    assert slack.posts[0]["channel"] == "drafts"
    card = rows(db, "slack_cards")[0]
    assert (card["channel"], card["slack_ts"], card["status"]) == ("C0TEST", "1000.1", "posted")
    db2, slack2 = FakeDb(), FakeSlack()
    run(make_deps(db2, FakeClassifier(llm("out_of_office", 0.9)), slack2, config=cfg))
    assert slack2.posts[0]["channel"] == "alerts"


def test_no_channel_configured_leaves_the_event_for_a_retry() -> None:
    db, slack = FakeDb(), FakeSlack()
    deps = make_deps(db, slack=slack, config=IngestConfig())
    outcome = run(deps)
    event = rows(db, "raw_events")[0]
    assert outcome.status == "failed" and not event.get("processed")
    assert "SLACK_DRAFT_CHANNEL" in event["process_error"] and slack.posts == []
    deps.config = IngestConfig(draft_channel="reply-bot-test")
    assert process_reply_event(deps, "e1", EVENT).classified
    assert len(slack.posts) == 1 and len(rows(db, "classifications")) == 1


# ------------------------------------------------------------------ not twice, not for follow-ups


def two_reply_history() -> dict[str, Any]:
    history = copy.deepcopy(HISTORY)
    second = copy.deepcopy(HISTORY["history"][1])
    second.update(time="2026-10-22T09:00:00.000Z", message_id="<reply-2@example.com>")
    second["email_body"] = "One more question."
    history["history"].append(second)
    return history


def test_a_follow_up_reply_is_stored_but_not_classified_and_not_posted() -> None:
    db, classifier, slack, smartlead = FakeDb(), FakeClassifier(), FakeSlack(), FakeSmartlead()
    deps = make_deps(db, classifier, slack, smartlead)
    assert run(deps).status == FIRST_REPLY
    smartlead.history = two_reply_history()
    outcome = run(deps, {**EVENT, "time_replied": "2026-10-22T09:00:00Z"}, "e2")
    assert outcome.status == FOLLOW_UP and not outcome.classified
    assert len(classifier.calls) == 1 and len(slack.posts) == 1
    assert len(rows(db, "classifications")) == 1 and len(rows(db, "slack_cards")) == 1
    assert len([m for m in rows(db, "messages") if m["direction"] == "REPLY"]) == 2
    assert rows(db, "raw_events")[1]["processed"] is True


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, IngestDeps]]:
    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GMC_CAMPAIGN_IDS", "101")
    get_settings.cache_clear()
    deps = make_deps(FakeDb())
    app.state.ingest = deps
    yield TestClient(app), deps
    del app.state.ingest
    get_settings.cache_clear()


def test_a_duplicate_event_does_not_classify_or_post_twice(
    server: tuple[TestClient, IngestDeps],
) -> None:
    client, deps = server
    assert post(client, EVENT).json() == {"status": "accepted"}
    assert post(client, EVENT).json() == {"status": "duplicate"}
    assert len(deps.classifier.calls) == 1 and len(deps.slack.posts) == 1
    assert len(rows(deps.db, "slack_cards")) == 1 and len(rows(deps.db, "classifications")) == 1


def test_ignored_events_post_nothing(server: tuple[TestClient, IngestDeps]) -> None:
    client, deps = server
    assert post(client, {**EVENT, "event_type": "EMAIL_SENT"}).json()["status"] == "ignored"
    assert post(client, {**EVENT, "campaign_id": 999}).json()["status"] == "ignored"
    assert (
        deps.classifier.calls == [] and deps.slack.posts == [] and not rows(deps.db, "slack_cards")
    )


def test_a_parse_failure_posts_nothing(server: tuple[TestClient, IngestDeps]) -> None:
    client, deps = server
    deps.smartlead.history = {"history": [{"type": "FORWARD"}]}
    assert post(client, EVENT).status_code == 200
    assert deps.classifier.calls == [] and deps.slack.posts == []
    assert not rows(deps.db, "slack_cards") and not rows(deps.db, "classifications")


def test_a_failed_fetch_posts_nothing(server: tuple[TestClient, IngestDeps]) -> None:
    client, deps = server
    deps.smartlead.error = SmartleadError("HTTP 500")
    assert post(client, EVENT).status_code == 200
    assert deps.classifier.calls == [] and deps.slack.posts == []
    assert not rows(deps.db, "raw_events")[0].get("processed")


def test_a_slack_failure_keeps_the_event_and_a_retry_posts_once_without_reclassifying(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    db, classifier, slack = FakeDb(), FakeClassifier(), FakeSlack(fail=True)
    deps = make_deps(db, classifier, slack)
    outcome = run(deps)
    event = rows(db, "raw_events")[0]
    assert outcome.status == "failed" and not event.get("processed")
    assert event["process_error"] == "classify or Slack step failed: RuntimeError"
    assert len(rows(db, "classifications")) == 1 and not rows(db, "slack_cards")
    assert "slack is down" not in caplog.text
    slack.fail = False
    assert process_reply_event(deps, "e1", EVENT).classified
    assert (
        len(classifier.calls) == 1
        and len(slack.posts) == 1
        and len(rows(db, "classifications")) == 1
    )
    assert rows(db, "raw_events")[0]["processed"] is True


def test_a_retry_after_the_action_was_recorded_does_not_record_it_twice() -> None:
    db, slack = FakeDb(), FakeSlack(fail=True)
    result = Classification(
        "do_not_contact", 1.0, "rule: asks to unsubscribe", "rule", rule_hit="unsubscribe"
    )
    deps = make_deps(db, FakeClassifier(result), slack)
    run(deps)
    slack.fail = False
    process_reply_event(deps, "e1", EVENT)
    assert len(rows(db, "actions")) == 1 and len(slack.posts) == 1


# ------------------------------------------------------------------ clicks only record


def card_row(db: FakeDb, status: str = "posted") -> None:
    db.tables.setdefault("slack_cards", []).append(
        {"id": "card-1", "channel": "C0TEST", "slack_ts": "1000.1", "status": status}
    )


def test_approve_and_skip_record_the_decision_and_nothing_else() -> None:
    db = FakeDb()
    card_row(db)
    assert record_card_click(db, "C0TEST", "1000.1", "reply_approve")
    assert rows(db, "slack_cards")[0]["status"] == "approved"
    assert set(db.tables) == {"slack_cards"}  # no drafts, actions, suppressions or sends
    db2 = FakeDb()
    card_row(db2)
    assert record_card_click(db2, "C0TEST", "1000.1", "reply_skip")
    assert rows(db2, "slack_cards")[0]["status"] == "skipped"


def test_edit_changes_nothing_and_the_first_click_wins() -> None:
    db = FakeDb()
    card_row(db)
    assert not record_card_click(db, "C0TEST", "1000.1", "reply_edit")
    assert rows(db, "slack_cards")[0]["status"] == "posted"
    assert record_card_click(db, "C0TEST", "1000.1", "reply_approve")
    assert not record_card_click(db, "C0TEST", "1000.1", "reply_skip")
    assert rows(db, "slack_cards")[0]["status"] == "approved"


def test_clicks_on_unknown_cards_are_ignored() -> None:
    db = FakeDb()
    card_row(db)
    assert not record_card_click(db, "C0TEST", "9999.9", "reply_approve")
    assert not record_card_click(db, "COTHER", "1000.1", "reply_approve")


def test_the_click_handler_updates_the_message_then_records() -> None:
    calls: list[tuple[str, str, str]] = []
    acked, updates = [], []

    async def ack() -> None:
        acked.append(True)

    class Client:
        async def chat_update(self, **kwargs: Any) -> None:
            updates.append(kwargs)

    body = {
        "message": {"ts": "1000.1", "blocks": [{"type": "section"}, {"type": "actions"}]},
        "channel": {"id": "C0TEST"},
        "user": {"id": "U1"},
    }
    record = lambda channel, ts, action: calls.append((channel, ts, action)) or True  # noqa: E731
    asyncio.run(handle_card_click("reply_approve", ack, body, Client(), record))
    assert acked and len(updates) == 1 and calls == [("C0TEST", "1000.1", "reply_approve")]


def test_a_recording_failure_does_not_break_the_click(caplog: pytest.LogCaptureFixture) -> None:
    def record(channel: str, ts: str, action: str) -> bool:
        raise RuntimeError("database down")

    async def ack() -> None:
        return None

    class Client:
        async def chat_update(self, **kwargs: Any) -> None:
            return None

    body = {"message": {"ts": "1.1", "blocks": []}, "channel": {"id": "C"}, "user": {"id": "U"}}
    asyncio.run(handle_card_click("reply_approve", ack, body, Client(), record))
    assert "could not record card action" in caplog.text and "database down" not in caplog.text


def test_settings_build_the_ingest_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)
    for key, value in GOOD_VALUES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SLACK_DRAFT_CHANNEL", "reply-bot-test")
    monkeypatch.setenv("CLASSIFIER_MIN_CONFIDENCE", "0.8")
    get_settings.cache_clear()
    cfg = IngestConfig.from_settings(get_settings())
    assert (cfg.min_confidence, cfg.min_confidence_not_interested) == (0.8, 0.90)
    assert cfg.draft_channel == "reply-bot-test" and cfg.alert_channel is None
    get_settings.cache_clear()
