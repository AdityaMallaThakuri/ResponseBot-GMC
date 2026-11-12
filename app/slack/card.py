"""The Approve / Edit / Skip card: Block Kit builders and the test-card poster."""

from dataclasses import dataclass
from typing import Any

from slack_sdk import WebClient

from app.config.settings import get_settings

APPROVE = "reply_approve"
EDIT = "reply_edit"
SKIP = "reply_skip"
ACTIONS_BLOCK_ID = "reply_card_actions"
GOOD_DRAFT = "draft_good"
BAD_DRAFT = "draft_bad"
FEEDBACK_BLOCK_ID = "draft_feedback"
FEEDBACK_TEXT = {GOOD_DRAFT: "marked this a good draft", BAD_DRAFT: "marked this a bad draft"}

# action id -> (button label, what the updated message says happened)
CHOICES: dict[str, tuple[str, str]] = {
    APPROVE: ("Approve", "approved this"),
    EDIT: ("Edit", "asked to edit this (editing is not built yet)"),
    SKIP: ("Skip", "skipped this"),
}

EXCERPT_LIMIT = 300

Block = dict[str, Any]


@dataclass(frozen=True)
class ThreadSummary:
    thread_ref: str  # opaque reference carried in each button's value
    lead_name: str
    campaign: str
    reply_excerpt: str
    company: str | None = None
    label: str | None = None


def escape_mrkdwn(text: str) -> str:
    """Slack treats &, < and > as control characters in mrkdwn."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _excerpt(text: str, limit: int = EXCERPT_LIMIT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _button(action_id: str, value: str, style: str | None = None) -> Block:
    button: Block = {
        "type": "button",
        "action_id": action_id,
        "text": {"type": "plain_text", "text": CHOICES[action_id][0]},
        "value": value,
    }
    if style:
        button["style"] = style
    return button


def build_card_blocks(summary: ThreadSummary) -> list[Block]:
    who = f"*{escape_mrkdwn(summary.lead_name)}*"
    if summary.company:
        who += f" at {escape_mrkdwn(summary.company)}"
    details = [escape_mrkdwn(summary.campaign)]
    if summary.label:
        details.append(f"label: {escape_mrkdwn(summary.label)}")
    quote = f">{escape_mrkdwn(_excerpt(summary.reply_excerpt))}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": who}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " | ".join(details)}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": quote}},
        {
            "type": "actions",
            "block_id": ACTIONS_BLOCK_ID,
            "elements": [
                _button(APPROVE, summary.thread_ref, "primary"),
                _button(EDIT, summary.thread_ref),
                _button(SKIP, summary.thread_ref),
            ],
        },
    ]


def fallback_text(summary: ThreadSummary) -> str:
    """Notification text. Deliberately has no part of the email body."""
    return f"New reply from {summary.lead_name} ({summary.campaign})"


def resolved_blocks(original: list[Block], user_id: str, action_id: str) -> list[Block]:
    """The card after a click: buttons removed, plus who chose what."""
    if action_id not in CHOICES:
        raise ValueError(f"unknown card action: {action_id!r}")
    kept = [block for block in original if block.get("type") != "actions"]
    outcome = f"<@{user_id}> {CHOICES[action_id][1]}"
    return [*kept, {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]}]


def post_test_card(
    channel: str, thread_summary: ThreadSummary, *, client: WebClient | None = None
) -> str:
    """Post the card to `channel` (name or id) and return the message ts."""
    slack = client or WebClient(token=get_settings().SLACK_BOT_TOKEN)
    response = slack.chat_postMessage(
        channel=channel,
        text=fallback_text(thread_summary),
        blocks=build_card_blocks(thread_summary),
    )
    return response["ts"]


# -- the card for a classified reply (C2) ----------------------------------------------------

REPLY_LIMIT = 600
LANE_TEXT = {
    "draft": "draft",
    "human_only": "human only",
    "would_suppress": "DO NOT REPLY (unsubscribe request; nothing was sent or changed)",
}


@dataclass(frozen=True)
class ReplyCard:
    thread_ref: str  # the thread id, carried in each button's value
    lead_name: str
    campaign: str
    reply: str
    lane: str  # draft, human_only or would_suppress
    label: str | None  # None when the classifier failed
    confidence: float | None
    reason: str
    company: str | None = None
    note: str | None = None  # for example "suggest: close"
    # The draft reply, if one was made. A person copies it and sends it from Smartlead.
    draft_text: str | None = None
    draft_flags: tuple[str, ...] = ()  # numbers, prices and percentages in the draft
    draft_notes: tuple[str, ...] = ()
    thread_url: str | None = None  # opens the thread in Smartlead's master inbox


DRAFT_LIMIT = 2800  # a Slack section holds 3000 characters
SMARTLEAD_THREAD_URL = "https://app.smartlead.ai/app/master-inbox?action=INBOX&leadMap={}"


def smartlead_thread_url(lead_map_id: object) -> str | None:
    """The master-inbox link for a thread (leadMap is the email_lead_map_id), or None."""
    try:
        number = int(lead_map_id)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return SMARTLEAD_THREAD_URL.format(number) if number > 0 else None


FENCE = "`" * 3


def lane_text(card: ReplyCard) -> str:
    if card.lane == "draft" and card.draft_text is not None:
        return "draft (copy it and send it yourself; the bot sends nothing)"
    return LANE_TEXT.get(card.lane, card.lane)


def code_block(text: str, limit: int = DRAFT_LIMIT) -> str:
    """The text in a code block, so Slack's copy button copies it exactly."""
    body = escape_mrkdwn(text.replace(FENCE, "'''"))
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return f"{FENCE}{body}{FENCE}"


def feedback_actions(thread_ref: str) -> Block:
    def button(action_id: str, label: str) -> Block:
        return {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": label},
            "value": thread_ref,
        }

    return {
        "type": "actions",
        "block_id": FEEDBACK_BLOCK_ID,
        "elements": [button(GOOD_DRAFT, "Good draft"), button(BAD_DRAFT, "Bad draft")],
    }


def feedback_resolved_blocks(original: list[Block], user_id: str, action_id: str) -> list[Block]:
    """The card after a feedback click: the two buttons replaced by who said what."""
    if action_id not in FEEDBACK_TEXT:
        raise ValueError(f"unknown feedback action: {action_id!r}")
    kept = [b for b in original if b.get("block_id") != FEEDBACK_BLOCK_ID]
    note = f"<@{user_id}> {FEEDBACK_TEXT[action_id]}"
    return [*kept, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]


def build_reply_card_blocks(card: ReplyCard) -> list[Block]:
    who = f"*{escape_mrkdwn(card.lead_name)}*"
    if card.company:
        who += f" at {escape_mrkdwn(card.company)}"
    lane = lane_text(card)
    prediction = (
        f"*Label:* `{escape_mrkdwn(card.label)}`  |  *Confidence:* {card.confidence:.2f}"
        if card.label is not None and card.confidence is not None
        else "*Label:* none (the classifier failed)"
    )
    blocks: list[Block] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": who}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"{escape_mrkdwn(card.campaign)} | lane: *{escape_mrkdwn(lane)}*",
                }
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f">{escape_mrkdwn(_excerpt(card.reply, REPLY_LIMIT))}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{prediction}\n*Reason:* {escape_mrkdwn(card.reason)}",
            },
        },
    ]
    if card.draft_text is not None:
        words = len(card.draft_text.split())
        text = f"*Draft reply* (copy it, {words} words)\n{code_block(card.draft_text)}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    if card.thread_url:
        link = f"<{escape_mrkdwn(card.thread_url)}|Open thread in Smartlead>"
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": link}]})
    if card.draft_flags:
        figures = ", ".join(f"`{escape_mrkdwn(f)}`" for f in card.draft_flags)
        warning = f":warning: *Check these figures before you send:* {figures}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": warning}})
    for line in card.draft_notes:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": escape_mrkdwn(line)}]}
        )
    if card.note:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*{escape_mrkdwn(card.note)}*"}],
            }
        )
    if card.draft_text is not None:
        blocks.append(feedback_actions(card.thread_ref))
    return blocks


def reply_fallback_text(card: ReplyCard) -> str:
    """Notification text. Deliberately has no part of the email body."""
    return f"New reply from {card.lead_name} ({card.campaign}): {lane_text(card)}"


def post_reply_card(slack: Any, channel: str, card: ReplyCard) -> tuple[str, str]:
    """Post the card. Returns (message ts, channel id): button clicks identify the card by both."""
    response = slack.chat_postMessage(
        channel=channel, text=reply_fallback_text(card), blocks=build_reply_card_blocks(card)
    )
    return response["ts"], response.get("channel") or channel


# -- the outcome of a send (C4) ---------------------------------------------------------------

RETRY_SEND = "reply_retry_send"
SEND_OUTCOME_BLOCK_ID = "send_outcome"
SEND_ACTIONS_BLOCK_ID = "send_retry_actions"


def with_send_outcome(blocks: list[Block], text: str, retry_ref: str | None = None) -> list[Block]:
    """The card with one line saying what happened to the send, and a Retry button if it can be."""
    kept = [
        b
        for b in blocks
        if b.get("block_id") not in (SEND_OUTCOME_BLOCK_ID, SEND_ACTIONS_BLOCK_ID)
        and b.get("type") != "actions"
    ]
    kept.append(
        {
            "type": "context",
            "block_id": SEND_OUTCOME_BLOCK_ID,
            "elements": [{"type": "mrkdwn", "text": f"*{escape_mrkdwn(text)}*"}],
        }
    )
    if retry_ref:
        kept.append(
            {
                "type": "actions",
                "block_id": SEND_ACTIONS_BLOCK_ID,
                "elements": [
                    {
                        "type": "button",
                        "action_id": RETRY_SEND,
                        "text": {"type": "plain_text", "text": "Retry send"},
                        "value": retry_ref,
                    }
                ],
            }
        )
    return kept
