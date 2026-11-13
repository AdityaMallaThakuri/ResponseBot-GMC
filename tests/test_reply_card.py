"""The Slack card for a classified reply (Block Kit)."""

import json
from typing import Any

import pytest

from app.ingest.draft_step import view_from_row
from app.slack.card import (
    NO_UNFURL,
    ReplyCard,
    build_reply_card_blocks,
    card_kind,
    feedback_resolved_blocks,
    notice_line,
    post_reply_card,
    reply_fallback_text,
    smartlead_thread_url,
)

URL = "https://app.smartlead.ai/app/master-inbox?action=INBOX&leadMap=777"


def card(**over: Any) -> ReplyCard:
    base: dict[str, Any] = {
        "thread_ref": "thread-1",
        "lead_name": "Jane Doe",
        "company": "Acme Capital",
        "campaign": "GMC - Lenders",
        "reply": "Sounds interesting, tell me more.",
        "lane": "draft",
        "label": "info_request",
        "confidence": 0.824,
        "reason": "Asks how it works.",
        "draft_text": "Hey Jane,\n\nHappy to explain.",
        "thread_url": URL,
    }
    return ReplyCard(**{**base, **over})


def text_of(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def actions(blocks: list[dict[str, Any]]) -> list[str]:
    found = [b for b in blocks if b["type"] == "actions"]
    return [e["action_id"] for e in found[0]["elements"]] if found else []


# ------------------------------------------------------------------ the draft card


def test_a_draft_card_is_title_quote_draft_words_actions() -> None:
    blocks = build_reply_card_blocks(card())
    assert [b["type"] for b in blocks] == ["section", "rich_text", "section", "context", "actions"]
    assert blocks[0]["text"]["text"] == "*Jane Doe · GMC - Lenders · info request*"
    quote = blocks[1]["elements"][0]
    assert quote["type"] == "rich_text_quote"
    assert quote["elements"] == [{"type": "text", "text": "Sounds interesting, tell me more."}]
    fence = "`" * 3
    assert blocks[2]["text"]["text"] == f"{fence}Hey Jane,\n\nHappy to explain.{fence}"
    assert blocks[3]["elements"][0]["text"] == "5 words"
    assert actions(blocks) == ["open_thread", "draft_good", "draft_bad"]


def test_the_card_has_no_reason_confidence_lane_or_label_code() -> None:
    text = text_of(build_reply_card_blocks(card()))
    for gone in ("Reason", "Confidence", "0.82", "Asks how it works", "lane", "`info_request`"):
        assert gone not in text
    assert "Low confidence" not in text and ":warning:" not in text


def test_the_buttons_are_open_in_smartlead_good_and_bad() -> None:
    (row,) = [b for b in build_reply_card_blocks(card()) if b["type"] == "actions"]
    assert [e["text"]["text"] for e in row["elements"]] == [
        "Open in Smartlead",
        "Good draft",
        "Bad draft",
    ]
    assert row["elements"][0]["url"] == URL and "value" not in row["elements"][0]
    assert {e["value"] for e in row["elements"][1:]} == {"thread-1"}
    assert row["block_id"] == "draft_feedback"
    assert "Open in Smartlead" in text_of(build_reply_card_blocks(card())) and URL in text_of(
        build_reply_card_blocks(card())
    ).replace("&amp;", "&")


def test_without_a_thread_url_there_is_no_open_button() -> None:
    assert actions(build_reply_card_blocks(card(thread_url=None))) == ["draft_good", "draft_bad"]


def test_the_quote_is_cleaned_cut_and_plain_text() -> None:
    reply = (
        "No thank you, we are fine.\n\nJan Sample\n\nManaging Partner\n\nSample Capital\n\n"
        "(555) 010-0101\n\nwww.sample.example"
    )
    quote = build_reply_card_blocks(card(reply=reply))[1]["elements"][0]["elements"][0]
    assert quote == {"type": "text", "text": "No thank you, we are fine."}  # signature gone
    long = build_reply_card_blocks(card(reply="word " * 400))[1]["elements"][0]["elements"][0]
    assert len(long["text"]) <= 400 and long["text"].endswith("…")
    phone = "call me at 555-010-0105 or see https://x.example/a <!channel>"
    plain = build_reply_card_blocks(card(reply=phone))[1]["elements"][0]["elements"][0]
    assert plain == {"type": "text", "text": phone}  # a text element: Slack links nothing


def test_a_long_draft_is_cut_and_escaped() -> None:
    blocks = build_reply_card_blocks(card(draft_text="x" * 5000))
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
    escaped = text_of(build_reply_card_blocks(card(draft_text="a <b>&</b>")))
    assert "a &lt;b&gt;&amp;&lt;/b&gt;" in escaped and "<b>" not in escaped


def test_one_small_warning_line_only_when_needed() -> None:
    warned = card(
        draft_notes=("Fill in [TIME OPTIONS]", "Price not confirmed: check with Aryan"),
        draft_flags=("$2M", "15%"),
    )
    blocks = build_reply_card_blocks(warned)
    lines = [b for b in blocks if b["type"] == "context" and ":warning:" in text_of([b])]
    assert len(lines) == 1
    text = lines[0]["elements"][0]["text"]
    assert "Fill in [TIME OPTIONS]" in text and "Check these figures: $2M, 15%" in text
    assert "Price not confirmed" in text


def test_low_confidence_adds_one_small_line() -> None:
    text = text_of(build_reply_card_blocks(card(low_confidence=True)))
    assert text.count("Low confidence: check the label") == 1


def test_price_not_confirmed_only_on_ma_when_there_is_no_price_sentence() -> None:
    def row(vertical: str) -> dict[str, Any]:
        return {
            "validator_ok": True,
            "template_id": "examples",
            "text": "Hey",
            "validator_result": {"price_confirmed": False, "vertical": vertical},
        }

    assert "Price not confirmed: check with Aryan" in view_from_row(row("ma"))[1].notes
    assert not any("Price" in n for n in view_from_row(row("lenders"))[1].notes)
    confirmed = row("ma")
    confirmed["validator_result"]["price_confirmed"] = True
    assert not any("Price" in n for n in view_from_row(confirmed)[1].notes)


# ------------------------------------------------------------------ no draft: Ray writes it


def test_a_card_that_needs_a_human_says_ray_to_write_this_one() -> None:
    blocks = build_reply_card_blocks(card(lane="human_only", draft_text=None, label="off_script"))
    assert card_kind(card(lane="human_only", draft_text=None, label="off_script")) == "human"
    assert blocks[0]["text"]["text"] == "*Jane Doe · GMC - Lenders · off script*"
    assert "Ray to write this one" in text_of(blocks)
    assert actions(blocks) == ["open_thread"] and "draft_good" not in text_of(blocks)
    assert "words" not in text_of(blocks)


def test_an_objection_with_no_matching_template_looks_the_same() -> None:
    plain = card(draft_text=None, label="objection_info", lane="human_only")
    assert "Ray to write this one" in text_of(build_reply_card_blocks(plain))
    assert card_kind(plain) == "human"


def test_a_classifier_failure_shows_no_label_and_a_warning() -> None:
    blocks = build_reply_card_blocks(
        card(
            label=None,
            confidence=None,
            draft_text=None,
            lane="human_only",
            draft_notes=("Classifier error: check the thread",),
        )
    )
    text = text_of(blocks)
    assert "no label" in text and "Classifier error: check the thread" in text
    assert "Ray to write this one" in text


# ------------------------------------------------------------------ one line, no action


def notice(label: str, reply: str = "x", **over: Any) -> ReplyCard:
    return card(label=label, reply=reply, draft_text=None, lane="human_only", **over)


@pytest.mark.parametrize(
    ("label", "reply", "over", "line"),
    [
        (
            "out_of_office",
            "I am on vacation, returning to work Monday, November 30.",
            {"lead_name": "Rob Example", "campaign": "PAVING RELOCATION 27/10"},
            "🔕 *Rob Example* · out of office until Nov 30 · PAVING RELOCATION 27/10",
        ),
        ("out_of_office", "I am away.", {}, "🔕 *Jane Doe* · out of office · GMC - Lenders"),
        (
            "do_not_contact",
            "stop",
            {},
            "⛔ *Jane Doe* · unsubscribe: do not reply · GMC - Lenders",
        ),
        (
            "not_interested",
            "no thanks",
            {"note": "suggest: close"},
            "👋 *Jane Doe* · not interested, suggest: close · GMC - Lenders",
        ),
        ("not_interested", "no thanks", {}, "👋 *Jane Doe* · not interested · GMC - Lenders"),
        (
            "wrong_person",
            "Wrong guy, please email Alice Sample at alice@sample.example.",
            {},
            "↪️ *Jane Doe* · wrong person, try Alice Sample · GMC - Lenders",
        ),
        ("wrong_person", "Wrong guy.", {}, "↪️ *Jane Doe* · wrong person · GMC - Lenders"),
        (
            "auto_reply_or_bounce",
            "do not reply",
            {},
            "🤖 *Jane Doe* · automatic reply or bounce · GMC - Lenders",
        ),
    ],
)
def test_no_action_cards_are_one_line(label: str, reply: str, over: dict, line: str) -> None:
    assert notice_line(notice(label, reply, **over)) == line
    blocks = build_reply_card_blocks(notice(label, reply, **over))
    assert len(blocks) == 1 and blocks[0]["text"]["text"] == line
    assert card_kind(notice(label)) == "notice"
    assert blocks[0]["accessory"]["url"] == URL
    assert blocks[0]["accessory"]["text"]["text"] == "Open in Smartlead"


def test_a_notice_without_a_thread_url_has_no_button_and_flags_low_confidence() -> None:
    blocks = build_reply_card_blocks(notice("out_of_office", thread_url=None, low_confidence=True))
    assert "accessory" not in blocks[0]
    assert "low confidence: check the label" in blocks[0]["text"]["text"]


# ------------------------------------------------------------------ posting and clicks


def test_the_notification_text_has_no_email_body() -> None:
    fallback = reply_fallback_text(card(reply="SECRET BODY TEXT"))
    assert "SECRET BODY TEXT" not in fallback
    assert fallback == "Jane Doe · info request · GMC - Lenders"


def test_posting_returns_the_ts_the_channel_id_and_turns_previews_off() -> None:
    class Slack:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802
            self.kwargs = kwargs
            return {"ts": "1.2", "channel": "C123"}

    slack = Slack()
    assert post_reply_card(slack, "reply-bot-test", card()) == ("1.2", "C123")
    assert slack.kwargs["channel"] == "reply-bot-test" and slack.kwargs["blocks"]
    assert NO_UNFURL == {"unfurl_links": False, "unfurl_media": False}
    assert slack.kwargs["unfurl_links"] is False and slack.kwargs["unfurl_media"] is False

    class NoChannel:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802
            return {"ts": "3.4"}

    assert post_reply_card(NoChannel(), "named", card()) == ("3.4", "named")


def test_a_feedback_click_keeps_the_open_button() -> None:
    blocks = build_reply_card_blocks(card())
    after = feedback_resolved_blocks(blocks, "U1", "draft_good")
    assert actions(after) == ["open_thread"]
    assert "<@U1> marked this a good draft" in text_of(after)
    plain = feedback_resolved_blocks(
        build_reply_card_blocks(card(thread_url=None)), "U1", "draft_bad"
    )
    assert actions(plain) == []


def test_what_we_actually_sent_is_a_small_section_at_the_bottom_for_script_cards_only() -> None:
    assert "actually sent" not in text_of(build_reply_card_blocks(card()))
    blocks = build_reply_card_blocks(card(actually_sent="Hi Jane,\n\n" + "x" * 700))
    assert blocks[-2]["type"] == "divider" and blocks[-1]["type"] == "context"
    sent = blocks[-1]["elements"][0]["text"]
    assert sent.startswith("*What we actually sent:* Hi Jane, xxx") and sent.count("x") <= 600
    notice_blocks = build_reply_card_blocks(notice("out_of_office", actually_sent="Hi"))
    assert len(notice_blocks) == 1  # a one-line card stays one line


def test_the_thread_link() -> None:
    url = smartlead_thread_url("3966392897")
    assert url == "https://app.smartlead.ai/app/master-inbox?action=INBOX&leadMap=3966392897"
    assert [smartlead_thread_url(v) for v in (None, "", "abc", 0, -3)] == [None] * 5
    assert smartlead_thread_url(42).endswith("leadMap=42")


def test_the_one_line_notes_of_the_webhook_job_never_unfurl() -> None:
    from types import SimpleNamespace

    from app.main import slack_note_poster

    sent: list[dict[str, Any]] = []

    class Slack:
        def chat_postMessage(self, **kwargs: Any) -> None:  # noqa: N802
            sent.append(kwargs)

    settings = SimpleNamespace(SLACK_ALERT_CHANNEL="alerts", SLACK_DRAFT_CHANNEL="drafts")
    slack_note_poster(settings, Slack())("Watching 3 campaigns")
    assert sent[0]["channel"] == "alerts"
    assert sent[0]["unfurl_links"] is False and sent[0]["unfurl_media"] is False
