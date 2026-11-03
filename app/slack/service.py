"""What a click on a draft card does (C3). Everything here is a database read or write.

- Approve: records the human decision (decisions row, draft and card status). Sending is C4, so
  nothing is sent. Refused while the draft has a placeholder such as [TIME OPTIONS], or if the
  draft failed validation.
- Skip: records the decision as skipped.
- Edit: opens a window with the draft text. Saving re-checks links, prices and numbers and shows
  warnings; an edit is never blocked. Approve still refuses while a placeholder remains.

Cards without a draft (the C2 kind) keep the old behaviour: Approve and Skip only set the card
status. The first decision wins.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.db.client import (
    Database,
    Row,
    fetch_classifications,
    fetch_messages,
    find_slack_card,
    get_draft,
    get_thread,
    insert_decision,
    update_draft,
    update_slack_card,
)
from app.db.records import CardStatus, Decision, DraftStatus, NewDecision
from app.ingest.draft_step import DraftView, notes_for
from app.slack.card import APPROVE, SKIP, ReplyCard, build_reply_card_blocks, reply_fallback_text
from app.slack.decisions import record_card_click
from app.templates.facts import Facts
from app.templates.playbook import Playbook
from app.validate.validator import (
    ValidationContext,
    check_edited_text,
    explain,
    placeholders,
)

EDIT_CALLBACK_ID = "draft_edit_submit"
TEXT_BLOCK_ID = "draft_text"
TEXT_ACTION_ID = "text"
MODAL_TEXT_LIMIT = 3000  # Slack's limit for a text input's initial value
SLACK_USER = re.compile(r"^[UW][A-Z0-9]{2,}$")


@dataclass(frozen=True)
class ClickResult:
    kind: str  # "recorded", "ignored", "blocked" or "unknown" (not a card we stored)
    message: str | None = None


@dataclass(frozen=True)
class EditResult:
    blocks: list[dict[str, Any]]
    text: str  # the notification text for chat_update
    warnings: list[str]


def edit_distance_pct(before: str, after: str) -> float:
    """How much of the text changed, 0 to 100."""
    return round((1 - difflib.SequenceMatcher(None, before, after).ratio()) * 100, 2)


class CardService:
    def __init__(self, get_db: Callable[[], Database], playbook: Playbook, facts: Facts) -> None:
        self._get_db = get_db
        self.playbook = playbook
        self.facts = facts

    @property
    def db(self) -> Database:
        return self._get_db()

    # -- lookups -------------------------------------------------------------------------

    def _open_card(self, channel: str, ts: str) -> Row | None:
        card = find_slack_card(self.db, channel, ts)
        return card if card and card.get("status") == CardStatus.POSTED.value else None

    def _known(self, channel: str, ts: str) -> bool:
        return find_slack_card(self.db, channel, ts) is not None

    # -- Approve and Skip ----------------------------------------------------------------

    def click(self, channel: str, ts: str, action_id: str, user_id: str) -> ClickResult:
        card = self._open_card(channel, ts)
        if card is None:
            # a card we never stored (a test card) behaves as before: the message is updated
            return ClickResult("ignored" if self._known(channel, ts) else "unknown")
        if not card.get("draft_id"):
            recorded = record_card_click(self.db, channel, ts, action_id)
            return ClickResult("recorded" if recorded else "ignored")
        if action_id not in (APPROVE, SKIP) or not SLACK_USER.match(user_id or ""):
            return ClickResult("ignored")
        draft = get_draft(self.db, card["draft_id"])
        if draft is None:
            return ClickResult("ignored")
        text = draft.get("final_text") or draft["text"]

        if action_id == SKIP:
            decision, status, card_status = (
                Decision.SKIPPED,
                DraftStatus.SKIPPED,
                CardStatus.SKIPPED,
            )
            new = NewDecision(draft["id"], user_id, decision)
        else:
            if not draft.get("validator_ok", True):
                return ClickResult(
                    "blocked", "This draft failed validation and cannot be approved."
                )
            left = placeholders(text)
            if left:
                return ClickResult("blocked", explain(f"placeholder:{left[0]}"))
            edited = bool(draft.get("human_edited"))
            decision = Decision.EDITED if edited else Decision.APPROVED
            status, card_status = DraftStatus.APPROVED, CardStatus.APPROVED
            new = NewDecision(
                draft["id"],
                user_id,
                decision,
                final_text=text,
                edit_distance_pct=edit_distance_pct(draft["text"], text) if edited else 0.0,
            )
        if not insert_decision(self.db, new).is_new:
            return ClickResult("ignored")  # someone else decided first
        update_draft(self.db, draft["id"], {"status": status.value})
        update_slack_card(self.db, card["id"], {"status": card_status.value})
        return ClickResult("recorded")

    # -- Edit ----------------------------------------------------------------------------

    def edit_view(self, channel: str, ts: str) -> dict[str, Any] | None:
        """The Edit window, or None if this card has no editable draft."""
        card = self._open_card(channel, ts)
        if card is None or not card.get("draft_id"):
            return None
        draft = get_draft(self.db, card["draft_id"])
        if draft is None or not draft.get("validator_ok", True):
            return None
        text = draft.get("final_text") or draft["text"]
        if len(text) > MODAL_TEXT_LIMIT:
            return None
        return {
            "type": "modal",
            "callback_id": EDIT_CALLBACK_ID,
            "private_metadata": json.dumps({"channel": channel, "ts": ts}),
            "title": {"type": "plain_text", "text": "Edit draft"},
            "submit": {"type": "plain_text", "text": "Save"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": TEXT_BLOCK_ID,
                    "label": {"type": "plain_text", "text": "Reply text"},
                    "hint": {
                        "type": "plain_text",
                        "text": "Links, prices and numbers are re-checked on save (warning only).",
                    },
                    "element": {
                        "type": "plain_text_input",
                        "action_id": TEXT_ACTION_ID,
                        "multiline": True,
                        "initial_value": text,
                    },
                }
            ],
        }

    def submit_edit(self, channel: str, ts: str, text: str) -> EditResult | None:
        """Save an edit, re-check it, and return the card to show. None if the card is gone."""
        card = self._open_card(channel, ts)
        if card is None or not card.get("draft_id"):
            return None
        draft = get_draft(self.db, card["draft_id"])
        thread = get_thread(self.db, card["thread_id"]) if draft else None
        if draft is None or thread is None:
            return None
        classification = (fetch_classifications(self.db, thread["id"]) or [{}])[-1]
        replies = [m for m in fetch_messages(self.db, thread["id"]) if m["direction"] == "REPLY"]
        sent = [m for m in fetch_messages(self.db, thread["id"]) if m["direction"] == "SENT"]
        reply_text = replies[0]["body_clean"] if replies else ""
        template = self.playbook.templates.get(draft["template_id"])
        ctx = ValidationContext(
            label=classification.get("label", ""),
            confidence=float(classification.get("confidence", 0)),
            min_confidence=0.0,
            reply=reply_text,
            outreach=sent[0]["body_clean"] if sent else "",
            campaign_name=str(thread.get("campaign_name") or ""),
            facts=self.facts,
        )
        if template is not None:
            result = check_edited_text(text, template, ctx)
            blockers, warnings = result.approve_blockers, result.warnings
        else:  # the template was removed from the playbook: only placeholders can be checked
            blockers, warnings = [f"placeholder:{p}" for p in placeholders(text)], []
        changed = text != draft["text"]
        update_draft(
            self.db,
            draft["id"],
            {
                "final_text": text,
                "human_edited": changed,
                "status": DraftStatus.EDITED.value if changed else DraftStatus.PROPOSED.value,
                "validator_result": {
                    "ok": True,
                    "blocking": [],
                    "approve_blockers": blockers,
                    "warnings": warnings,
                },
            },
        )
        view = DraftView(
            text, draft["template_id"], changed, notes_for(blockers, warnings), not blockers
        )
        name = " ".join(
            p for p in (thread.get("lead_first_name"), thread.get("lead_last_name")) if p
        )
        shown = ReplyCard(
            thread_ref=thread["id"],
            lead_name=name or thread["lead_email"],
            company=thread.get("company"),
            campaign=str(thread.get("campaign_name") or "(unknown)"),
            reply=reply_text,
            lane="draft",
            label=classification.get("label"),
            confidence=float(classification["confidence"])
            if "confidence" in classification
            else None,
            reason=classification.get("reason", ""),
            buttons=True,
            draft_text=view.text,
            template_id=view.template_id,
            draft_edited=view.edited,
            draft_notes=view.notes,
            approve_allowed=view.approve_allowed,
        )
        return EditResult(build_reply_card_blocks(shown), reply_fallback_text(shown), warnings)
