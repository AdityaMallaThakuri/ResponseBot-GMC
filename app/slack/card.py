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


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_LIMIT else flat[: EXCERPT_LIMIT - 1] + "…"


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
