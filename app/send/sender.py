"""C4: send an approved draft through Smartlead, behind every lock.

A reply is sent only if ALL of these hold; the first one that fails is recorded in `actions`
(type send, details.result = blocked_...) and nothing is sent:

  1. SEND_ENABLED is true (the Smartlead client refuses otherwise, as a second check).
  2. A person approved it: a `decisions` row exists (decided_by is a Slack user id) and the draft
     is still `approved`. The bot never approves.
  3. No placeholder such as [TIME OPTIONS] remains in the final text.
  4. The validator on the final text, including edits, finds no link, price or number outside the
     template wording and the approved facts.
  5. It is addressed to the right lead and campaign: the thread's lead is the sender of the reply
     we answer, and the campaign is one of GMC_CAMPAIGN_IDS.
  6. The recipient is in SEND_ALLOWLIST, unless LIVE_SEND_ALL is on (it must stay off until we
     decide to go live). The Smartlead client checks this again.
  7. The draft has not been sent: the draft moves approved -> sent in one conditional UPDATE, so
     two clicks (or two workers) cannot both send. A failed send moves it back to approved, so it
     can be retried.

The email body is never logged or stored in `actions`; only ids and a result code are.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.db.client import (
    Database,
    Row,
    claim_draft,
    fetch_actions,
    fetch_decision,
    fetch_messages,
    fetch_slack_cards,
    get_draft,
    get_thread,
    insert_action,
    update_draft,
    update_slack_card,
)
from app.db.records import ActionType, CardStatus, NewAction
from app.ingest.deps import utc_now
from app.smartlead.errors import RecipientNotAllowed, SendDisabledError
from app.smartlead.models import Lead, Thread
from app.templates.facts import Facts
from app.templates.playbook import Playbook
from app.validate.context import validation_context
from app.validate.validator import check_edited_text, placeholders

logger = logging.getLogger(__name__)

SLACK_USER = re.compile(r"^[UW][A-Z0-9]{2,}$")
SENT, FAILED, BLOCKED = "sent", "failed", "blocked"

# result code -> plain words for the card
WORDS = {
    "sent": "Sent",
    "failed": "Send failed",
    "blocked_send_disabled": "Not sent: sending is switched off (SEND_ENABLED is false)",
    "blocked_not_allowlisted": "Not sent: the recipient is not in SEND_ALLOWLIST",
    "blocked_not_approved": "Not sent: no person has approved this draft",
    "blocked_placeholder": "Not sent: a placeholder such as [TIME OPTIONS] is still in the text",
    "blocked_claims": "Not sent: the text has a link, price or number that is not approved",
    "blocked_template_missing": "Not sent: the draft's template is no longer in the playbook",
    "blocked_wrong_recipient": "Not sent: the reply is not addressed to the lead who wrote",
    "blocked_wrong_campaign": "Not sent: the campaign is not one we handle",
    "blocked_no_stats_id": "Not sent: there is no message to reply to",
    "blocked_already_sent": "Not sent again: this draft was already sent",
    "blocked_no_draft": "Not sent: the draft was not found",
}
# blocked for a reason that a person can fix, so a retry makes sense
RETRYABLE = {
    "failed",
    "blocked_send_disabled",
    "blocked_not_allowlisted",
    "blocked_claims",
    "blocked_placeholder",
}


@dataclass(frozen=True)
class SendConfig:
    send_enabled: bool = False
    allowlist: frozenset[str] = frozenset()
    live_send_all: bool = False
    campaign_ids: frozenset[int] = frozenset()

    @classmethod
    def from_settings(cls, settings: Any) -> SendConfig:
        return cls(
            send_enabled=settings.SEND_ENABLED,
            allowlist=settings.send_allowlist,
            live_send_all=settings.LIVE_SEND_ALL,
            campaign_ids=settings.gmc_campaign_ids,
        )


@dataclass(frozen=True)
class SendResult:
    status: str  # sent, failed or blocked
    code: str  # sent, failed, or blocked_<reason>
    text: str  # plain words for the card, never the email body
    recipient: str | None = None  # masked

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE


def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    head, _, tld = domain.rpartition(".")
    return f"{local[:1]}***@{head[:1]}***.{tld}" if domain else "***"


def to_email_html(text: str) -> str:
    """Plain text to simple HTML: paragraphs on blank lines, line breaks inside them.

    Unverified: that Smartlead's reply route wants HTML in email_body. The first live test shows it.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paragraphs)


class SendService:
    def __init__(
        self,
        get_db: Callable[[], Database],
        get_smartlead: Callable[[], Any],
        playbook: Playbook,
        facts: Facts,
        config: SendConfig,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._get_db = get_db
        self._get_smartlead = get_smartlead
        self.playbook = playbook
        self.facts = facts
        self.config = config
        self.clock = clock

    @property
    def db(self) -> Database:
        return self._get_db()

    # -- recording -----------------------------------------------------------------------

    def _record(
        self, thread_id: str, draft_id: str, code: str, triggered_by: str, **extra: Any
    ) -> None:
        details = {"result": code, "draft_id": draft_id, **extra}
        insert_action(
            self.db,
            NewAction(
                thread_id=thread_id,
                type=ActionType.SEND,
                triggered_by=triggered_by,
                reversible=False,
                details=details,
            ),
        )

    def _result(self, code: str, recipient: str | None = None, detail: str = "") -> SendResult:
        status = SENT if code == SENT else FAILED if code == FAILED else BLOCKED
        text = WORDS.get(code, code) + (f" ({detail})" if detail else "")
        if recipient and code == SENT:
            text += f" to {recipient}"
        return SendResult(status, code, text, recipient)

    def _blocked(
        self, thread_id: str, draft_id: str, code: str, by: str, **extra: Any
    ) -> SendResult:
        self._record(thread_id, draft_id, code, by, **extra)
        logger.info("send blocked for draft %s: %s", draft_id, code)
        return self._result(code)

    def _card_for(self, thread_id: str, draft_id: str) -> Row | None:
        return next(
            (c for c in fetch_slack_cards(self.db, thread_id) if c.get("draft_id") == draft_id),
            None,
        )

    # -- the checks, then the send ------------------------------------------------------

    def send_draft(self, draft_id: str, triggered_by: str) -> SendResult:
        draft = get_draft(self.db, draft_id)
        if draft is None:
            return self._result("blocked_no_draft")
        thread = get_thread(self.db, draft["thread_id"])
        if thread is None:
            return self._result("blocked_no_draft")
        tid = thread["id"]

        def block(code: str, **extra: Any) -> SendResult:
            return self._blocked(tid, draft_id, code, triggered_by, **extra)

        already = [
            a
            for a in fetch_actions(self.db, tid)
            if a["type"] == ActionType.SEND.value
            and (a.get("details") or {}).get("draft_id") == draft_id
            and (a.get("details") or {}).get("result") == SENT
        ]
        if already or draft.get("status") == "sent":
            return block("blocked_already_sent")
        if not self.config.send_enabled:
            return block("blocked_send_disabled")

        decision = fetch_decision(self.db, draft_id)
        approved_by_person = (
            decision is not None
            and decision.get("decision") in ("approved", "edited")
            and bool(SLACK_USER.match(str(decision.get("decided_by") or "")))
        )
        if not approved_by_person or draft.get("status") != "approved":
            return block("blocked_not_approved")

        text = draft.get("final_text") or draft["text"]
        if placeholders(text):
            return block("blocked_placeholder")

        template = self.playbook.templates.get(draft["template_id"])
        if template is None:
            return block("blocked_template_missing")
        if not draft.get("validator_ok", True):
            return block("blocked_claims", why="draft failed validation")
        ctx = validation_context(self.db, thread, self.facts)
        checked = check_edited_text(text, template, ctx)
        bad = [
            w for w in checked.warnings if w.startswith(("link_not_allowed", "number_not_allowed"))
        ]
        if bad:
            return block("blocked_claims", why=[w.split(":")[0] for w in bad])

        replies = [m for m in fetch_messages(self.db, tid) if m["direction"] == "REPLY"]
        replies.sort(key=lambda m: m["sent_at"])
        latest = replies[-1] if replies else None
        lead_email = str(thread.get("lead_email") or "").strip().lower()
        if (
            latest is None
            or not lead_email
            or str(latest["from_address"]).strip().lower() != lead_email
        ):
            return block("blocked_wrong_recipient")
        campaign_id = thread.get("email_campaign_id")
        if campaign_id not in self.config.campaign_ids:
            return block("blocked_wrong_campaign", campaign_id=campaign_id)
        stats_id = str(latest.get("stats_id") or "")
        if not stats_id:
            return block("blocked_no_stats_id")
        if not self.config.live_send_all and lead_email not in self.config.allowlist:
            return block("blocked_not_allowlisted", recipient=mask_email(lead_email))

        if not claim_draft(self.db, draft_id, "approved", "sent"):
            return block("blocked_already_sent")  # someone else sent (or is sending) it

        masked = mask_email(lead_email)
        smartlead_thread = Thread(
            lead=Lead(
                campaign_id=int(campaign_id), lead_id=int(thread["email_lead_id"]), email=lead_email
            ),
            reply_to_stats_id=stats_id,
            to_email=lead_email,
        )
        try:
            self._get_smartlead().send_reply(smartlead_thread, to_email_html(text))
        except (RecipientNotAllowed, SendDisabledError) as exc:
            update_draft(self.db, draft_id, {"status": "approved"})
            code = (
                "blocked_not_allowlisted"
                if isinstance(exc, RecipientNotAllowed)
                else "blocked_send_disabled"
            )
            return block(code, recipient=masked)
        except Exception as exc:  # noqa: BLE001 - record the type only; never the body or the response
            update_draft(self.db, draft_id, {"status": "approved"})  # retryable
            error = type(exc).__name__
            self._record(tid, draft_id, FAILED, triggered_by, error=error, recipient=masked)
            card = self._card_for(tid, draft_id)
            if card:
                update_slack_card(self.db, card["id"], {"status": CardStatus.FAILED.value})
            logger.error("send failed for draft %s: %s", draft_id, error)
            return self._result(FAILED, detail=error)

        self._record(
            tid,
            draft_id,
            SENT,
            triggered_by,
            recipient=masked,
            campaign_id=campaign_id,
            reply_to_stats_id=stats_id,
            template_id=draft["template_id"],
        )
        card = self._card_for(tid, draft_id)
        if card:
            update_slack_card(self.db, card["id"], {"status": CardStatus.RESOLVED.value})
        logger.info("draft %s sent (template %s)", draft_id, draft["template_id"])
        return self._result(SENT, recipient=masked)
