"""C3 end to end: draft in the pipeline, the card, Approve / Skip / Edit. Synthetic data only."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.classify.classifier import Classification
from app.ingest.deps import IngestConfig, IngestDeps
from app.ingest.process import process_reply_event
from app.slack.card import ReplyCard, build_reply_card_blocks
from app.slack.handlers import handle_edit_submission, handle_service_click
from app.slack.service import (
    EDIT_CALLBACK_ID,
    TEXT_ACTION_ID,
    TEXT_BLOCK_ID,
    CardService,
    edit_distance_pct,
)
from app.templates.engine import Drafter
from tests.c3_support import FACTS_DATA, FakeModel, make_facts, make_playbook, template_data
from tests.fakedb import FakeDb
from tests.test_c2 import FakeClassifier, FakeSlack, llm
from tests.test_drafting import topic_model
from tests.test_webhook import EVENT, NOW, FakeSmartlead, rows

USER = "U012ABCDEF"
CHANNEL, TS = "C0TEST", "1000.1"


def make_deps(
    label: str = "info_request",
    confidence: float = 0.9,
    facts=None,
    playbook=None,
    slack: FakeSlack | None = None,
    draft_min: float = 0.75,
    model: FakeModel | None = None,
) -> IngestDeps:
    facts = facts or make_facts()
    return IngestDeps(
        db=FakeDb(),
        smartlead=FakeSmartlead(),
        clock=lambda: NOW,
        classifier=FakeClassifier(llm(label, confidence)),
        slack=slack or FakeSlack(),
        drafter=Drafter(
            playbook or make_playbook(facts), facts, model or topic_model("send times"), "model-x"
        ),
        config=IngestConfig(draft_channel="drafts", draft_min_confidence=draft_min),
    )


def run(deps: IngestDeps, event_id: str = "e1"):
    deps.db.tables.setdefault("raw_events", []).append({"id": event_id, "processed": False})
    return process_reply_event(deps, event_id, EVENT)


def block_types(post: dict[str, Any]) -> list[str]:
    return [b["type"] for b in post["blocks"]]


def buttons(post: dict[str, Any]) -> list[str]:
    actions = [b for b in post["blocks"] if b["type"] == "actions"]
    return [e["action_id"] for e in actions[0]["elements"]] if actions else []


def text_of(post: dict[str, Any]) -> str:
    return json.dumps(post["blocks"], ensure_ascii=False)


# ------------------------------------------------------------------ the pipeline makes a draft


def test_a_draft_is_stored_with_template_slots_validator_result_and_model() -> None:
    deps = make_deps("info_request")
    run(deps)
    (draft,) = rows(deps.db, "drafts")
    assert (draft["template_id"], draft["template_version"]) == ("T2", "synthetic-1")
    assert draft["slots"] == {"NAME": "Jane", "TOPIC": "send times"}
    assert (
        draft["text"]
        == "SYNTHETIC T2: Hello Jane. Test wording only. Thanks for asking about send times."
    )
    assert draft["validator_ok"] is True and draft["validator_reasons"] == []
    assert draft["validator_result"] == {
        "ok": True,
        "blocking": [],
        "approve_blockers": [],
        "warnings": [],
    }
    assert (draft["model"], draft["status"]) == ("model-x", "proposed")
    assert draft["classification_id"] == rows(deps.db, "classifications")[0]["id"]


def test_the_card_shows_the_draft_and_the_three_buttons() -> None:
    deps = make_deps("info_request")
    run(deps)
    (post,) = deps.slack.posts
    text = text_of(post)
    assert (
        "Draft reply" in text
        and "template T2" in text
        and "Thanks for asking about send times." in text
    )
    assert buttons(post) == ["reply_approve", "reply_edit", "reply_skip"]
    card = rows(deps.db, "slack_cards")[0]
    assert card["lane"] == "draft" and card["draft_id"] == rows(deps.db, "drafts")[0]["id"]
    assert post["channel"] == "drafts" and "Thanks for asking" not in post["text"]


def test_a_draft_with_a_placeholder_has_no_approve_button() -> None:
    deps = make_deps("meeting_request")
    run(deps)
    (post,) = deps.slack.posts
    assert buttons(post) == ["reply_edit", "reply_skip"]
    assert "Approve is blocked until [TIME OPTIONS] is filled in" in text_of(post)
    assert "[TIME OPTIONS]" in text_of(post)
    assert rows(deps.db, "drafts")[0]["validator_result"]["approve_blockers"] == [
        "placeholder:[TIME OPTIONS]"
    ]


def test_a_blocked_draft_is_stored_as_failed_and_goes_to_human_only() -> None:
    unapproved = make_facts(
        claims=[{**FACTS_DATA["claims"][0], "approved": False}, *FACTS_DATA["claims"][1:]]
    )
    deps = make_deps("meeting_request", facts=unapproved)
    run(deps)
    (draft,) = rows(deps.db, "drafts")
    assert draft["validator_ok"] is False and draft["status"] == "failed"
    assert "claim_not_usable:calendar_link" in draft["validator_reasons"]
    assert "claim_not_usable:calendar_link" in draft["validator_result"]["blocking"]
    (post,) = deps.slack.posts
    text = text_of(post)
    assert (
        "human only" in text
        and "Draft blocked:" in text
        and "not approved for this campaign" in text
    )
    assert "Draft reply" not in text and buttons(post) == []  # the blocked text is not shown
    card = rows(deps.db, "slack_cards")[0]
    assert card["lane"] == "human_only" and card["draft_id"] == draft["id"]


def test_no_template_for_the_label_goes_to_human_only() -> None:
    only_t1 = make_playbook(templates={"T1": template_data("T1")})
    deps = make_deps("info_request", playbook=only_t1)
    run(deps)
    assert not rows(deps.db, "drafts")
    (post,) = deps.slack.posts
    assert "No draft: no approved template for info_request" in text_of(post)
    assert buttons(post) == [] and "human only" in text_of(post)
    assert rows(deps.db, "slack_cards")[0]["lane"] == "human_only"


def test_below_the_draft_threshold_goes_to_human_only() -> None:
    deps = make_deps("info_request", confidence=0.8, draft_min=0.85)
    run(deps)
    assert not rows(deps.db, "drafts")
    assert "No draft: confidence is below the draft threshold" in text_of(deps.slack.posts[0])


@pytest.mark.parametrize("label", ["out_of_office", "wrong_person", "not_interested", "off_script"])
def test_labels_outside_the_draft_lane_are_never_drafted(label: str) -> None:
    deps = make_deps(label, confidence=0.99)
    run(deps)
    assert not rows(deps.db, "drafts") and buttons(deps.slack.posts[0]) == []


def test_do_not_contact_is_never_drafted() -> None:
    deps = make_deps()
    deps.classifier = FakeClassifier(
        Classification(
            "do_not_contact", 1.0, "rule: asks to unsubscribe", "rule", rule_hit="unsubscribe"
        )
    )
    run(deps)
    assert not rows(deps.db, "drafts") and rows(deps.db, "actions")[0]["type"] == "would_suppress"


def test_a_retry_after_a_slack_failure_reuses_the_stored_draft() -> None:
    slack = FakeSlack(fail=True)
    model = topic_model("send times")
    deps = make_deps("info_request", slack=slack, model=model)
    run(deps)
    slack.fail = False
    process_reply_event(deps, "e1", EVENT)
    assert len(rows(deps.db, "drafts")) == 1 and len(slack.posts) == 1
    assert len(model.calls) == 1  # the model filled the slots once


def test_with_no_drafter_the_card_is_the_c2_card() -> None:
    deps = make_deps("info_request")
    deps.drafter = None
    run(deps)
    assert not rows(deps.db, "drafts") and "Draft reply" not in text_of(deps.slack.posts[0])
    assert buttons(deps.slack.posts[0]) == ["reply_approve", "reply_edit", "reply_skip"]


# ------------------------------------------------------------------ Approve, Skip, Edit


def service_for(deps: IngestDeps) -> CardService:
    return CardService(lambda: deps.db, deps.drafter.playbook, deps.drafter.facts)


def card_ids(deps: IngestDeps) -> tuple[str, str]:
    card = rows(deps.db, "slack_cards")[0]
    return card["channel"], card["slack_ts"]


def test_approve_records_the_decision_and_nothing_else() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    result = service_for(deps).click(channel, ts, "reply_approve", USER)
    assert result.kind == "recorded"
    (decision,) = rows(deps.db, "decisions")
    draft = rows(deps.db, "drafts")[0]
    assert (decision["decided_by"], decision["decision"]) == (USER, "approved")
    assert decision["final_text"] == draft["text"] and decision["edit_distance_pct"] == 0.0
    assert draft["status"] == "approved" and rows(deps.db, "slack_cards")[0]["status"] == "approved"
    assert not rows(deps.db, "actions") or all(
        a["type"] != "send" for a in rows(deps.db, "actions")
    )
    assert not rows(deps.db, "suppressions")


def test_approve_is_refused_while_a_placeholder_remains() -> None:
    deps = make_deps("meeting_request")
    run(deps)
    channel, ts = card_ids(deps)
    result = service_for(deps).click(channel, ts, "reply_approve", USER)
    assert result.kind == "blocked" and "[TIME OPTIONS]" in result.message
    assert not rows(deps.db, "decisions") and rows(deps.db, "slack_cards")[0]["status"] == "posted"


def test_approve_is_refused_for_a_draft_that_failed_validation() -> None:
    unapproved = make_facts(
        claims=[{**FACTS_DATA["claims"][0], "approved": False}, *FACTS_DATA["claims"][1:]]
    )
    deps = make_deps("meeting_request", facts=unapproved)
    run(deps)
    channel, ts = card_ids(deps)
    result = service_for(deps).click(channel, ts, "reply_approve", USER)
    assert result.kind == "blocked" and "failed validation" in result.message
    assert not rows(deps.db, "decisions")


def test_skip_records_a_skipped_decision() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    assert service_for(deps).click(channel, ts, "reply_skip", USER).kind == "recorded"
    (decision,) = rows(deps.db, "decisions")
    assert decision["decision"] == "skipped" and "final_text" not in decision
    assert rows(deps.db, "drafts")[0]["status"] == "skipped"
    assert rows(deps.db, "slack_cards")[0]["status"] == "skipped"


def test_the_first_click_wins_and_odd_users_are_ignored() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    service = service_for(deps)
    assert service.click(channel, ts, "reply_approve", "not-a-slack-id").kind == "ignored"
    assert service.click(channel, ts, "reply_approve", USER).kind == "recorded"
    assert service.click(channel, ts, "reply_skip", USER).kind == "ignored"
    assert (
        len(rows(deps.db, "decisions")) == 1
        and rows(deps.db, "slack_cards")[0]["status"] == "approved"
    )


def test_a_card_we_never_stored_is_unknown() -> None:
    deps = make_deps("info_request")
    assert service_for(deps).click("C0X", "9.9", "reply_approve", USER).kind == "unknown"


def test_the_edit_window_holds_the_current_text() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    view = service_for(deps).edit_view(channel, ts)
    assert view["type"] == "modal" and view["callback_id"] == EDIT_CALLBACK_ID
    assert json.loads(view["private_metadata"]) == {"channel": channel, "ts": ts}
    element = view["blocks"][0]["element"]
    assert (
        element["multiline"] is True
        and element["initial_value"] == rows(deps.db, "drafts")[0]["text"]
    )
    assert view["blocks"][0]["block_id"] == TEXT_BLOCK_ID and element["action_id"] == TEXT_ACTION_ID


def test_no_edit_window_without_a_draft_or_for_a_failed_draft() -> None:
    unapproved = make_facts(
        claims=[{**FACTS_DATA["claims"][0], "approved": False}, *FACTS_DATA["claims"][1:]]
    )
    blocked = make_deps("meeting_request", facts=unapproved)
    run(blocked)
    assert service_for(blocked).edit_view(*card_ids(blocked)) is None
    plain = make_deps("info_request")
    plain.drafter = None  # a C2 card has no draft
    run(plain)
    c2_service = CardService(lambda: plain.db, make_playbook(), make_facts())
    assert rows(plain.db, "slack_cards")[0].get("draft_id") is None
    assert c2_service.edit_view(*card_ids(plain)) is None


def test_an_edit_is_stored_revalidated_and_only_warned() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    service = service_for(deps)
    edited = "Totally new: we closed 17 deals, see https://evil.example/x"
    result = service.submit_edit(channel, ts, edited)
    assert (
        "number_not_allowed:17" in result.warnings
        and "link_not_allowed:https://evil.example/x" in result.warnings
    )
    draft = rows(deps.db, "drafts")[0]
    assert (
        draft["final_text"] == edited
        and draft["human_edited"] is True
        and draft["status"] == "edited"
    )
    assert (
        draft["validator_ok"] is True and draft["validator_result"]["warnings"] == result.warnings
    )
    card_text = json.dumps(result.blocks)
    assert (
        "Totally new" in card_text
        and "edited" in card_text
        and "Warning: the number 17" in card_text
    )
    assert any(b["type"] == "actions" for b in result.blocks)  # Approve is still possible


def test_approving_an_edited_draft_records_edited_with_the_distance() -> None:
    deps = make_deps("meeting_request")
    run(deps)
    channel, ts = card_ids(deps)
    service = service_for(deps)
    filled = rows(deps.db, "drafts")[0]["text"].replace(
        "[TIME OPTIONS]", "Tuesday 10am or Wednesday 2pm"
    )
    result = service.submit_edit(channel, ts, filled)
    assert result.warnings == []
    assert any(
        b.get("type") == "actions" and "reply_approve" in json.dumps(b) for b in result.blocks
    )
    assert service.click(channel, ts, "reply_approve", USER).kind == "recorded"
    (decision,) = rows(deps.db, "decisions")
    assert decision["decision"] == "edited" and decision["final_text"] == filled
    assert 0 < decision["edit_distance_pct"] <= 100
    assert rows(deps.db, "drafts")[0]["status"] == "approved"


def test_an_edit_that_keeps_a_placeholder_still_cannot_be_approved() -> None:
    deps = make_deps("meeting_request")
    run(deps)
    channel, ts = card_ids(deps)
    service = service_for(deps)
    result = service.submit_edit(channel, ts, "New text but [TIME OPTIONS] is still here")
    assert not any(
        "reply_approve" in json.dumps(b) for b in result.blocks if b["type"] == "actions"
    )
    assert service.click(channel, ts, "reply_approve", USER).kind == "blocked"


def test_saving_unchanged_text_does_not_mark_the_draft_edited() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    original = rows(deps.db, "drafts")[0]["text"]
    service_for(deps).submit_edit(channel, ts, original)
    draft = rows(deps.db, "drafts")[0]
    assert draft["human_edited"] is False and draft["status"] == "proposed"


def test_edit_distance() -> None:
    assert edit_distance_pct("same", "same") == 0.0
    assert edit_distance_pct("abcd", "wxyz") == 100.0
    assert 0 < edit_distance_pct("hello there", "hello world") < 100


# ------------------------------------------------------------------ the Slack handlers


class FakeSlackApi:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []
        self.views: list[dict[str, Any]] = []

    async def chat_update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)

    async def chat_postEphemeral(self, **kwargs: Any) -> None:  # noqa: N802
        self.ephemeral.append(kwargs)

    async def views_open(self, **kwargs: Any) -> None:
        self.views.append(kwargs)


async def noop_ack(**kwargs: Any) -> None:
    noop_ack.calls.append(kwargs)  # type: ignore[attr-defined]


noop_ack.calls = []  # type: ignore[attr-defined]


def click_body(channel: str, ts: str, blocks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "message": {"ts": ts, "blocks": blocks or [{"type": "section"}, {"type": "actions"}]},
        "channel": {"id": channel},
        "user": {"id": USER},
        "trigger_id": "trig-1",
    }


def test_the_approve_click_updates_the_message_after_recording() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(
        handle_service_click(
            "reply_approve", noop_ack, click_body(channel, ts), api, service_for(deps)
        )
    )
    assert len(rows(deps.db, "decisions")) == 1 and len(api.updates) == 1
    assert "approved" in json.dumps(api.updates[0]["blocks"]) and api.ephemeral == []


def test_a_blocked_approve_tells_only_the_clicker_and_changes_nothing() -> None:
    deps = make_deps("meeting_request")
    run(deps)
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(
        handle_service_click(
            "reply_approve", noop_ack, click_body(channel, ts), api, service_for(deps)
        )
    )
    assert api.updates == [] and "[TIME OPTIONS]" in api.ephemeral[0]["text"]
    assert api.ephemeral[0]["user"] == USER and not rows(deps.db, "decisions")


def test_the_edit_click_opens_the_window() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(
        handle_service_click(
            "reply_edit", noop_ack, click_body(channel, ts), api, service_for(deps)
        )
    )
    assert (
        api.views[0]["trigger_id"] == "trig-1"
        and api.views[0]["view"]["callback_id"] == EDIT_CALLBACK_ID
    )
    assert api.updates == [] and not rows(deps.db, "decisions")


def test_a_click_on_a_card_we_never_stored_still_updates_the_message() -> None:
    deps = make_deps("info_request")
    api = FakeSlackApi()
    asyncio.run(
        handle_service_click(
            "reply_skip", noop_ack, click_body("C0X", "9.9"), api, service_for(deps)
        )
    )
    assert len(api.updates) == 1 and not rows(deps.db, "decisions")


def submission(channel: str, ts: str, text: str) -> dict[str, Any]:
    return {
        "view": {
            "private_metadata": json.dumps({"channel": channel, "ts": ts}),
            "state": {"values": {TEXT_BLOCK_ID: {TEXT_ACTION_ID: {"value": text}}}},
        }
    }


def test_saving_the_edit_window_refreshes_the_card() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    api = FakeSlackApi()
    asyncio.run(
        handle_edit_submission(
            noop_ack, submission(channel, ts, "Edited wording here"), api, service_for(deps)
        )
    )
    assert rows(deps.db, "drafts")[0]["final_text"] == "Edited wording here"
    assert "Edited wording here" in json.dumps(api.updates[0]["blocks"])


def test_an_empty_edit_is_rejected_in_the_window() -> None:
    deps = make_deps("info_request")
    run(deps)
    channel, ts = card_ids(deps)
    acks: list[dict[str, Any]] = []

    async def ack(**kwargs: Any) -> None:
        acks.append(kwargs)

    api = FakeSlackApi()
    asyncio.run(handle_edit_submission(ack, submission(channel, ts, "   "), api, service_for(deps)))
    assert acks[0]["response_action"] == "errors" and TEXT_BLOCK_ID in acks[0]["errors"]
    assert api.updates == [] and rows(deps.db, "drafts")[0].get("final_text") is None


# ------------------------------------------------------------------ the card builder


def card(**over: Any) -> ReplyCard:
    base: dict[str, Any] = {
        "thread_ref": "t1",
        "lead_name": "Pat Sample",
        "campaign": "GMC - Synthetic",
        "reply": "Tell me more.",
        "lane": "draft",
        "label": "info_request",
        "confidence": 0.9,
        "reason": "Asks for more.",
        "buttons": True,
        "draft_text": "Line one\nLine two <b>&</b>",
        "template_id": "T2",
    }
    return ReplyCard(**{**base, **over})


def test_the_draft_is_quoted_escaped_and_labelled() -> None:
    text = json.dumps(build_reply_card_blocks(card()))
    assert "Draft reply" in text and "template T2" in text
    assert "&gt;Line one" not in text and "\\n>Line two &lt;b&gt;&amp;&lt;/b&gt;" in text
    assert "<b>" not in text


def test_an_edited_draft_says_so_and_a_very_long_one_is_cut() -> None:
    assert "edited" in json.dumps(build_reply_card_blocks(card(draft_edited=True)))
    blocks = build_reply_card_blocks(card(draft_text="x" * 5000))
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")


def test_the_approve_button_can_be_left_out() -> None:
    blocks = build_reply_card_blocks(
        card(approve_allowed=False, draft_notes=("Approve is blocked.",))
    )
    actions = next(b for b in blocks if b["type"] == "actions")
    assert [e["action_id"] for e in actions["elements"]] == ["reply_edit", "reply_skip"]
    assert "Approve is blocked." in json.dumps(blocks)


def test_the_helpers_import() -> None:
    assert SimpleNamespace and Classification and pytest
