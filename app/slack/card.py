"""The Approve / Edit / Skip card: Block Kit builders and the test-card poster."""

from dataclasses import dataclass
from typing import Any

from slack_sdk import WebClient

from app.config.settings import get_settings
from app.slack.quote import clean_quote, ooo_until, referral_name

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
NO_UNFURL = {"unfurl_links": False, "unfurl_media": False}  # no link previews, ever

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
        **NO_UNFURL,
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
    low_confidence: bool = False  # the label's confidence is below the classifier minimum
    # Only scripts/post_replies.py sets this (a reply already sent for an old thread). A live
    # card never has it.
    actually_sent: str | None = None


SENT_LIMIT = 600  # characters of "What we actually sent"
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


def feedback_actions(thread_ref: str, leading: list[Block] | None = None) -> Block:
    """The actions row of a draft card: [Open in Smartlead] [Good draft] [Bad draft]."""

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
        "elements": [
            *(leading or []),
            button(GOOD_DRAFT, "Good draft"),
            button(BAD_DRAFT, "Bad draft"),
        ],
    }


def feedback_resolved_blocks(original: list[Block], user_id: str, action_id: str) -> list[Block]:
    """The card after a feedback click: Good/Bad replaced by who said what; Open stays."""
    if action_id not in FEEDBACK_TEXT:
        raise ValueError(f"unknown feedback action: {action_id!r}")
    kept: list[Block] = []
    for block in original:
        if block.get("block_id") != FEEDBACK_BLOCK_ID:
            kept.append(block)
            continue
        links = [e for e in block.get("elements", []) if e.get("action_id") == OPEN_THREAD]
        if links:
            kept.append({"type": "actions", "block_id": OPEN_BLOCK_ID, "elements": links})
    note = f"<@{user_id}> {FEEDBACK_TEXT[action_id]}"
    return [*kept, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}]


LOW_CONFIDENCE_LINE = "Low confidence: check the label"
RAY_LINE = "Ray to write this one"
OPEN_THREAD = "open_thread"  # the "Open in Smartlead" button (a URL button; clicking only opens it)
OPEN_BLOCK_ID = "open_thread_actions"

# Replies nobody needs to answer: one line each, with a small Open button.
NOTICE_LABELS = frozenset(
    {"out_of_office", "auto_reply_or_bounce", "do_not_contact", "not_interested", "wrong_person"}
)


def label_words(label: str | None) -> str:
    return (label or "no label").replace("_", " ")


def card_kind(card: ReplyCard) -> str:
    """ "notice" (one line, no action), "draft" (a draft to copy) or "human" (Ray writes it)."""
    if card.label in NOTICE_LABELS:
        return "notice"
    return "draft" if card.draft_text is not None else "human"


def open_button(url: str) -> Block:
    return {
        "type": "button",
        "action_id": OPEN_THREAD,
        "text": {"type": "plain_text", "text": "Open in Smartlead"},
        "url": url,
    }


def notice_line(card: ReplyCard) -> str:
    """The single line of a no-action card, for example
    "🔕 *Rob Example* · out of office until Nov 30 · PAVING RELOCATION 27/10"."""
    who = f"*{escape_mrkdwn(card.lead_name)}*"
    if card.label == "out_of_office":
        until = ooo_until(card.reply)
        icon, what = "🔕", "out of office" + (f" until {until}" if until else "")
    elif card.label == "auto_reply_or_bounce":
        icon, what = "🤖", "automatic reply or bounce"
    elif card.label == "do_not_contact":
        icon, what = "⛔", "unsubscribe: do not reply"
    elif card.label == "not_interested":
        icon = "👋"
        what = (
            "not interested, suggest: close" if card.note == "suggest: close" else "not interested"
        )
    else:  # wrong_person
        referral = referral_name(card.reply)
        icon, what = "↪️", "wrong person" + (f", try {referral}" if referral else "")
    if card.low_confidence:
        what += ", low confidence: check the label"
    return f"{icon} {who} · {escape_mrkdwn(what)} · {escape_mrkdwn(card.campaign)}"


def _notice_blocks(card: ReplyCard) -> list[Block]:
    block: Block = {"type": "section", "text": {"type": "mrkdwn", "text": notice_line(card)}}
    if card.thread_url:
        block["accessory"] = open_button(card.thread_url)
    return [block]


def _quote_block(reply: str) -> Block:
    """The cleaned reply as a quote in a rich_text block: plain text, so Slack links nothing."""
    text = clean_quote(reply) or "(empty reply)"
    return {
        "type": "rich_text",
        "elements": [
            {"type": "rich_text_quote", "elements": [{"type": "text", "text": text}]},
        ],
    }


def _small(text: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def build_reply_card_blocks(card: ReplyCard) -> list[Block]:
    if card_kind(card) == "notice":
        return _notice_blocks(card)
    title = " · ".join(
        escape_mrkdwn(part) for part in (card.lead_name, card.campaign, label_words(card.label))
    )
    blocks: list[Block] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
        _quote_block(card.reply),
    ]
    if card.draft_text is not None:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": code_block(card.draft_text)}}
        )
        blocks.append(_small(f"{len(card.draft_text.split())} words"))
    else:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"_{RAY_LINE}_"}})
    warnings = list(card.draft_notes)
    if card.draft_flags:
        warnings.append("Check these figures: " + ", ".join(card.draft_flags))
    if card.low_confidence:
        warnings.insert(0, LOW_CONFIDENCE_LINE)
    if warnings:
        blocks.append(_small(":warning: " + " · ".join(escape_mrkdwn(w) for w in warnings)))
    buttons: list[Block] = [open_button(card.thread_url)] if card.thread_url else []
    if card.draft_text is not None:
        blocks.append(feedback_actions(card.thread_ref, buttons))
    elif buttons:
        blocks.append({"type": "actions", "block_id": OPEN_BLOCK_ID, "elements": buttons})
    if card.actually_sent:
        sent = " ".join(card.actually_sent.split())
        shown = sent if len(sent) <= SENT_LIMIT else sent[: SENT_LIMIT - 1] + "…"
        blocks.append({"type": "divider"})
        blocks.append(_small(f"*What we actually sent:* {escape_mrkdwn(shown)}"))
    return blocks


def reply_fallback_text(card: ReplyCard) -> str:
    """Notification text. Deliberately has no part of the email body."""
    return f"{card.lead_name} · {label_words(card.label)} · {card.campaign}"


def post_reply_card(slack: Any, channel: str, card: ReplyCard) -> tuple[str, str]:
    """Post the card. Returns (message ts, channel id): button clicks identify the card by both."""
    response = slack.chat_postMessage(
        channel=channel,
        text=reply_fallback_text(card),
        blocks=build_reply_card_blocks(card),
        **NO_UNFURL,
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
