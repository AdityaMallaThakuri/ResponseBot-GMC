"""The Approve / Edit / Skip card: Block Kit builders and the test-card poster."""

from dataclasses import dataclass
from typing import Any

from slack_sdk import WebClient

from app.config.settings import get_settings

APPROVE = "reply_approve"
EDIT = "reply_edit"
SKIP = "reply_skip"
ACTIONS_BLOCK_ID = "reply_card_actions"

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
    "draft": "draft (no draft yet: drafting is built in a later step)",
    "human_only": "human only",
    "would_suppress": "would_suppress (recorded only, nothing was sent)",
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
    buttons: bool = False


def build_reply_card_blocks(card: ReplyCard) -> list[Block]:
    who = f"*{escape_mrkdwn(card.lead_name)}*"
    if card.company:
        who += f" at {escape_mrkdwn(card.company)}"
    lane = LANE_TEXT.get(card.lane, card.lane)
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
    if card.note:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*{escape_mrkdwn(card.note)}*"}],
            }
        )
    if card.buttons:
        blocks.append(
            {
                "type": "actions",
                "block_id": ACTIONS_BLOCK_ID,
                "elements": [
                    _button(APPROVE, card.thread_ref, "primary"),
                    _button(EDIT, card.thread_ref),
                    _button(SKIP, card.thread_ref),
                ],
            }
        )
    return blocks


def reply_fallback_text(card: ReplyCard) -> str:
    """Notification text. Deliberately has no part of the email body."""
    return (
        f"New reply from {card.lead_name} ({card.campaign}): {LANE_TEXT.get(card.lane, card.lane)}"
    )


def post_reply_card(slack: Any, channel: str, card: ReplyCard) -> tuple[str, str]:
    """Post the card. Returns (message ts, channel id): button clicks identify the card by both."""
    response = slack.chat_postMessage(
        channel=channel, text=reply_fallback_text(card), blocks=build_reply_card_blocks(card)
    )
    return response["ts"], response.get("channel") or channel
