"""The Slack card for a classified reply."""

import json
from typing import Any

from app.slack.card import (
    REPLY_LIMIT,
    ReplyCard,
    build_reply_card_blocks,
    post_reply_card,
    reply_fallback_text,
)


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
        "buttons": True,
    }
    return ReplyCard(**{**base, **over})


def text_of(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def test_the_card_shows_everything_asked_for() -> None:
    text = text_of(build_reply_card_blocks(card()))
    for expected in (
        "*Jane Doe* at Acme Capital",
        "GMC - Lenders",
        "Sounds interesting, tell me more.",
        "`info_request`",
        "0.82",
        "Asks how it works.",
        "lane:",
    ):
        assert expected in text


def test_the_company_is_optional() -> None:
    blocks = build_reply_card_blocks(card(company=None))
    assert blocks[0]["text"]["text"] == "*Jane Doe*"


def test_the_reply_is_cut_at_600_characters() -> None:
    blocks = build_reply_card_blocks(card(reply="word " * 400))
    quote = blocks[2]["text"]["text"]
    assert REPLY_LIMIT == 600
    assert len(quote) <= 601 + 1 and quote.endswith("…")
    short = build_reply_card_blocks(card(reply="x" * 600))[2]["text"]["text"]
    assert short == ">" + "x" * 600  # exactly 600 is not cut


def test_slack_control_characters_in_the_reply_are_escaped() -> None:
    blocks = build_reply_card_blocks(card(reply="<!channel> a & b <http://x|y>"))
    quote = blocks[2]["text"]["text"]
    assert "<!channel>" not in quote and "&lt;!channel&gt;" in quote and "&amp;" in quote


def test_lane_wording() -> None:
    assert "human only" in text_of(build_reply_card_blocks(card(lane="human_only", buttons=False)))
    suppress = text_of(build_reply_card_blocks(card(lane="would_suppress", buttons=False)))
    assert "would_suppress" in suppress and "nothing was sent" in suppress
    assert "draft (no draft yet" in text_of(build_reply_card_blocks(card()))


def test_buttons_only_when_asked_and_all_carry_the_thread_ref() -> None:
    with_buttons = build_reply_card_blocks(card())
    actions = [b for b in with_buttons if b["type"] == "actions"]
    assert len(actions) == 1
    assert [e["action_id"] for e in actions[0]["elements"]] == [
        "reply_approve",
        "reply_edit",
        "reply_skip",
    ]
    assert {e["value"] for e in actions[0]["elements"]} == {"thread-1"}
    without = build_reply_card_blocks(card(buttons=False))
    assert all(b["type"] != "actions" for b in without)


def test_a_note_is_shown() -> None:
    blocks = build_reply_card_blocks(card(lane="human_only", buttons=False, note="suggest: close"))
    assert "*suggest: close*" in text_of(blocks)
    assert "suggest" not in text_of(build_reply_card_blocks(card()))


def test_a_classifier_failure_shows_no_label() -> None:
    text = text_of(
        build_reply_card_blocks(
            card(
                label=None,
                confidence=None,
                reason="classifier error",
                lane="human_only",
                buttons=False,
            )
        )
    )
    assert "the classifier failed" in text and "classifier error" in text


def test_the_notification_text_has_no_email_body() -> None:
    fallback = reply_fallback_text(card(reply="SECRET BODY TEXT"))
    assert "SECRET BODY TEXT" not in fallback
    assert "Jane Doe" in fallback and "GMC - Lenders" in fallback


def test_posting_returns_the_ts_and_the_channel_id() -> None:
    class Slack:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802
            self.kwargs = kwargs
            return {"ts": "1.2", "channel": "C123"}

    slack = Slack()
    assert post_reply_card(slack, "reply-bot-test", card()) == ("1.2", "C123")
    assert slack.kwargs["channel"] == "reply-bot-test" and slack.kwargs["blocks"]

    class NoChannel:
        def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802
            return {"ts": "3.4"}

    assert post_reply_card(NoChannel(), "named", card()) == ("3.4", "named")
