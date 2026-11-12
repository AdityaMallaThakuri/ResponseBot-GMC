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
    assert "human only" in text_of(build_reply_card_blocks(card(lane="human_only")))
    suppress = text_of(build_reply_card_blocks(card(lane="would_suppress")))
    assert "DO NOT REPLY" in suppress and "nothing was sent" in suppress
    shown = text_of(build_reply_card_blocks(card(draft_text="Hello")))
    assert "copy it and send it yourself" in shown


def test_the_draft_is_a_copy_friendly_code_block() -> None:
    fence = "`" * 3
    blocks = build_reply_card_blocks(
        card(draft_text=f"Hi Jane,\n\nSounds good <3 & more\n{fence}x{fence}")
    )
    shown = next(b for b in blocks if b["type"] == "section" and "Draft reply" in b["text"]["text"])
    body = shown["text"]["text"].split(" words)\n", 1)[1]
    assert body.startswith(fence) and body.endswith(fence)
    assert "&lt;3 &amp; more" in body and "'''x'''" in body  # escaped, no nested fence
    assert body.count(fence) == 2


def test_figures_in_the_draft_are_highlighted() -> None:
    flagged = card(draft_text="We charge $2M", draft_flags=("$2M", "15%"))
    text = text_of(build_reply_card_blocks(flagged))
    assert "Check these figures before you send" in text and "`$2M`" in text and "`15%`" in text
    assert "Check these figures" not in text_of(build_reply_card_blocks(card(draft_text="Hi")))


def test_only_a_draft_card_has_the_two_feedback_buttons_and_no_approve_or_skip() -> None:
    blocks = build_reply_card_blocks(card(draft_text="Hello"))
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    assert [e["action_id"] for e in actions[0]["elements"]] == ["draft_good", "draft_bad"]
    assert [e["text"]["text"] for e in actions[0]["elements"]] == ["Good draft", "Bad draft"]
    assert {e["value"] for e in actions[0]["elements"]} == {"thread-1"}
    assert "reply_approve" not in text_of(blocks) and "reply_skip" not in text_of(blocks)
    for lane in ("human_only", "would_suppress"):
        assert all(b["type"] != "actions" for b in build_reply_card_blocks(card(lane=lane)))
    assert all(b["type"] != "actions" for b in build_reply_card_blocks(card()))  # no draft


def test_a_note_is_shown() -> None:
    blocks = build_reply_card_blocks(card(lane="human_only", note="suggest: close"))
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


def test_the_card_links_to_the_thread_in_smartlead() -> None:
    from app.slack.card import ReplyCard, build_reply_card_blocks, smartlead_thread_url

    url = smartlead_thread_url("3966392897")
    assert url == "https://app.smartlead.ai/app/master-inbox?action=INBOX&leadMap=3966392897"
    card = ReplyCard(
        thread_ref="t",
        lead_name="A",
        campaign="GMC - X",
        reply="hi",
        lane="draft",
        label="info_request",
        confidence=0.9,
        reason="r",
        draft_text="Hey",
        thread_url=url,
    )
    text = str(build_reply_card_blocks(card))
    assert "<https://app.smartlead.ai/app/master-inbox?action=INBOX&amp;leadMap=3966392897" in text
    assert "Open thread in Smartlead" in text
    without = str(build_reply_card_blocks(ReplyCard(**{**card.__dict__, "thread_url": None})))
    assert "Open thread in Smartlead" not in without


def test_no_link_without_a_usable_lead_map_id() -> None:
    from app.slack.card import smartlead_thread_url

    assert [smartlead_thread_url(v) for v in (None, "", "abc", 0, -3)] == [None] * 5
    assert smartlead_thread_url(42).endswith("leadMap=42")
