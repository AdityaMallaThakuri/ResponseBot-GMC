"""The daily summary of replies nobody has to answer.

Out-of-office, automatic replies and bounces, unsubscribes (do_not_contact), not interested and
wrong person never get a card. When one arrives, ingest stores a `slack_cards` row with channel
"digest" and status "pending". Once a day (DIGEST_HOUR, UTC) one message goes to
SLACK_ALERT_CHANNEL, grouped by type with one entry per reply; each name links to the thread in
Smartlead. The rows then become "resolved", so nothing is listed twice. Without
SLACK_ALERT_CHANNEL the rows wait and nothing is posted. Nothing here sends, suppresses or calls
Smartlead. Never log an email body.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.db.client import (
    Database,
    Row,
    fetch_classifications,
    fetch_messages,
    fetch_pending_digest_cards,
    get_thread,
    update_slack_card,
)
from app.ingest.deps import IngestConfig
from app.slack.card import NO_UNFURL, Block, escape_mrkdwn, smartlead_thread_url
from app.slack.quote import ooo_until, referral_name

log = logging.getLogger(__name__)

DIGEST_CHANNEL = "digest"  # slack_cards.channel of a reply waiting for the summary
SECTION_LIMIT = 2800  # a Slack section holds 3000 characters

# (label, emoji, heading), in the order they appear in the summary
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("do_not_contact", "⛔", "Unsubscribe"),
    ("not_interested", "👋", "Not interested"),
    ("wrong_person", "↪️", "Wrong person"),
    ("out_of_office", "🔕", "Out of office"),
    ("auto_reply_or_bounce", "🤖", "Automatic reply or bounce"),
)


@dataclass(frozen=True)
class DigestEntry:
    label: str
    name: str
    campaign: str
    url: str | None = None
    detail: str = ""  # for example "until Nov 30", "try Alice Sample", "suggest: close"


def short_campaign(name: str) -> str:
    """ "GMC - M&A GOOGLE RESEND 04/11" -> "M&A GOOGLE RESEND 04/11"."""
    stripped = re.sub(r"^\s*GMC\s*[-–—:]\s*", "", name or "", flags=re.IGNORECASE).strip()
    return stripped or "(unknown)"


def entry_text(entry: DigestEntry) -> str:
    """ "<url|Paxton Smith> (M&A GOOGLE RESEND 04/11, until Nov 30)": the name is the link."""
    name = escape_mrkdwn(entry.name).replace("|", "/")
    who = f"<{entry.url}|{name}>" if entry.url else name
    inner = ", ".join(x for x in (short_campaign(entry.campaign), entry.detail) if x)
    return f"{who} ({escape_mrkdwn(inner)})"


def _chunks(entries: list[DigestEntry], limit: int = SECTION_LIMIT) -> list[list[str]]:
    """The entries' text in pieces that each fit one Slack section."""
    pieces: list[list[str]] = [[]]
    size = 0
    for item in (entry_text(e) for e in entries):
        if pieces[-1] and size + len(item) + 2 > limit:
            pieces.append([])
            size = 0
        pieces[-1].append(item)
        size += len(item) + 2
    return pieces


def build_digest(entries: list[DigestEntry]) -> tuple[str, list[Block]]:
    """(notification text, blocks) for one summary message. Needs at least one entry."""
    total = len(entries)
    title = f"No action needed: {total} {'reply' if total == 1 else 'replies'}"
    blocks: list[Block] = [{"type": "header", "text": {"type": "plain_text", "text": title}}]
    for label, emoji, heading in GROUPS:
        group = [e for e in entries if e.label == label]
        for n, piece in enumerate(_chunks(group) if group else []):
            head = f"{emoji} *{heading} ({len(group)}):*" if n == 0 else f"{emoji} *{heading}:*"
            text = f"{head} " + ", ".join(piece)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    return title, blocks


def _first_reply_text(db: Database, thread_id: str) -> str:
    replies = [m for m in fetch_messages(db, thread_id) if m["direction"] == "REPLY"]
    replies.sort(key=lambda m: str(m["sent_at"]))
    return str(replies[0]["body_clean"]) if replies else ""


def _lead_name(thread: Row) -> str:
    name = " ".join(p for p in (thread.get("lead_first_name"), thread.get("lead_last_name")) if p)
    return name or str(thread["lead_email"])


def entry_for(db: Database, card: Row, config: IngestConfig) -> DigestEntry | None:
    """The summary entry for a pending row, or None if its thread or classification is missing."""
    thread = get_thread(db, card["thread_id"])
    classifications = fetch_classifications(db, card["thread_id"])
    if thread is None or not classifications:
        return None
    result = classifications[-1]
    label = str(result["label"])
    reply = _first_reply_text(db, thread["id"])
    details: list[str] = []
    if label == "out_of_office" and (until := ooo_until(reply)):
        details.append(f"until {until}")
    if label == "wrong_person" and (referral := referral_name(reply)):
        details.append(f"try {referral}")
    confidence = float(result["confidence"])
    if label == "not_interested" and confidence >= config.min_confidence_not_interested:
        details.append("suggest: close")
    if confidence < config.min_confidence:
        details.append("low confidence: check the label")
    return DigestEntry(
        label=label,
        name=_lead_name(thread),
        campaign=str(thread.get("campaign_name") or ""),
        url=smartlead_thread_url(thread.get("email_lead_map_id")),
        detail=", ".join(details),
    )


def collect(db: Database, config: IngestConfig) -> list[tuple[Row, DigestEntry]]:
    """The waiting rows with their entries, oldest first."""
    found: list[tuple[Row, DigestEntry]] = []
    for card in fetch_pending_digest_cards(db, DIGEST_CHANNEL):
        entry = entry_for(db, card, config)
        if entry is None:
            log.warning("digest row %s has no thread or classification; left pending", card["id"])
        else:
            found.append((card, entry))
    return found


def post_digest(db: Database, slack: Any, channel: str | None, config: IngestConfig) -> int:
    """Post the summary now and mark its rows resolved. Returns how many replies it listed.

    Nothing is posted (and the rows stay pending) with no channel or nothing to list."""
    if not channel:
        log.info("SLACK_ALERT_CHANNEL is not set: the summary of no-action replies is not posted")
        return 0
    found = collect(db, config)
    if not found:
        return 0
    text, blocks = build_digest([entry for _, entry in found])
    slack.chat_postMessage(channel=channel, text=text, blocks=blocks, **NO_UNFURL)
    for card, _ in found:
        update_slack_card(db, card["id"], {"status": "resolved"})
    log.info("posted the daily summary: %d replies", len(found))
    return len(found)


def seconds_until_hour(hour: int, now: datetime) -> float:
    """Seconds from `now` to the next time the clock reads `hour`:00 (always more than 0)."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
