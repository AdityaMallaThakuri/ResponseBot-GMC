"""scripts/post_replies.py helpers, and the 'What we actually sent' card section (script only)."""

import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.slack.card import ReplyCard, build_reply_card_blocks
from app.smartlead.models import Direction, Message

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import post_replies  # noqa: E402

DAY = date(2026, 11, 12)


def msg(direction: Direction, day: int, hour: int, text: str) -> Message:
    return Message(
        direction,
        datetime(2026, 11, day, hour, tzinfo=UTC),
        text,
        "s",
        f"m{day}{hour}{direction.value}",
        "a@x.com",
        "b@x.com",
    )


SENT, REPLY = Direction.SENT, Direction.REPLY
STEP = "Hi, quick question about your deal flow. Are you taking on new mandates this quarter?"


def card(**over: Any) -> ReplyCard:
    base: dict[str, Any] = {
        "thread_ref": "t1",
        "lead_name": "Pat",
        "campaign": "GMC - M&A",
        "reply": "tell me more",
        "lane": "draft",
        "label": "info_request",
        "confidence": 0.9,
        "reason": "asks how it works",
        "draft_text": "Hey Pat, sure.",
    }
    return ReplyCard(**{**base, **over})


def test_a_live_card_has_no_actually_sent_section() -> None:
    assert "actually sent" not in str(build_reply_card_blocks(card()))


def test_the_script_card_shows_what_we_actually_sent_first_600_characters() -> None:
    blocks = build_reply_card_blocks(card(actually_sent="Hi Pat, " + "x" * 700))
    assert blocks[-2]["type"] == "divider"  # a small section at the bottom
    text = blocks[-1]["elements"][0]["text"]
    assert text.startswith("*What we actually sent:* Hi Pat,") and text.count("x") <= 600


def test_a_notice_card_can_show_it_too() -> None:
    blocks = build_reply_card_blocks(card(lane="human_only", draft_text=None, actually_sent="Hi"))
    assert "What we actually sent:" in str(blocks)


def test_our_answer_that_is_not_a_sequence_step_is_found() -> None:
    thread = [
        msg(SENT, 5, 9, STEP),
        msg(REPLY, 12, 10, "interested"),
        msg(SENT, 12, 15, "Hey Pat, happy to explain. Would Tuesday work?"),
    ]
    assert (
        post_replies.actually_sent_text(thread, [])
        == "Hey Pat, happy to explain. Would Tuesday work?"
    )


def test_a_sequence_step_sent_after_the_reply_is_not_our_answer() -> None:
    thread = [
        msg(SENT, 5, 9, STEP),
        msg(REPLY, 12, 10, "interested"),
        msg(SENT, 13, 9, STEP),
    ]
    assert post_replies.actually_sent_text(thread, [STEP]) is None
    assert post_replies.actually_sent_text(thread[:2], []) is None


def test_only_first_replies_of_the_day_without_a_card_are_posted() -> None:
    fresh = [msg(SENT, 5, 9, STEP), msg(REPLY, 12, 10, "hi")]
    assert post_replies.plan_item({}, fresh, DAY, False, None) == ("post", "new")
    assert post_replies.plan_item({}, fresh, DAY, False, "we answered")[0] == "post"
    assert post_replies.plan_item({}, fresh, DAY, True, None) == (
        "skip",
        "already has a Slack card",
    )
    follow_up = [msg(SENT, 5, 9, STEP), msg(REPLY, 10, 10, "hi"), msg(REPLY, 12, 11, "more")]
    action, why = post_replies.plan_item({}, follow_up, DAY, False, None)
    assert action == "skip" and "follow-up" in why
