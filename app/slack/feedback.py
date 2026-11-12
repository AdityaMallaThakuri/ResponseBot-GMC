"""The "Good draft" / "Bad draft" buttons. A click only records feedback on the draft.

Nothing here sends, edits or suppresses anything, and nothing calls Smartlead. The first click on
a card wins. Never log a Slack payload: it can carry email text.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi.concurrency import run_in_threadpool
from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.db.client import Database, find_slack_card, get_draft, update_draft
from app.slack.card import BAD_DRAFT, GOOD_DRAFT, feedback_resolved_blocks

log = logging.getLogger(__name__)

VALUE_FOR_ACTION = {GOOD_DRAFT: "good", BAD_DRAFT: "bad"}

# (channel id, message ts, action id, slack user id) -> True if the click was recorded
FeedbackRecorder = Callable[[str, str, str, str], bool]


def record_feedback(
    db: Database, channel: str, slack_ts: str, action_id: str, user_id: str
) -> bool:
    """Store the click on the card's draft. False for an unknown card or one already rated."""
    value = VALUE_FOR_ACTION.get(action_id)
    if value is None:
        return False
    card = find_slack_card(db, channel, slack_ts)
    if card is None or not card.get("draft_id"):
        return False
    draft = get_draft(db, card["draft_id"])
    if draft is None or draft.get("feedback"):
        return False
    update_draft(
        db,
        draft["id"],
        {"feedback": value, "feedback_by": user_id, "feedback_at": datetime.now(UTC).isoformat()},
    )
    return True


async def handle_feedback_click(
    action_id: str,
    ack: AsyncAck,
    body: dict[str, Any],
    client: AsyncWebClient,
    record: FeedbackRecorder,
) -> None:
    await ack()
    message = body.get("message") or {}
    channel = (body.get("channel") or {}).get("id")
    user_id = (body.get("user") or {}).get("id")
    ts = message.get("ts")
    if not (channel and user_id and ts):
        log.warning("feedback action %s arrived without a message, channel or user", action_id)
        return
    try:
        recorded = await run_in_threadpool(record, channel, ts, action_id, user_id)
    except Exception as exc:  # noqa: BLE001 - never log the payload
        log.warning("could not record feedback %s: %s", action_id, type(exc).__name__)
        return
    if not recorded:
        return
    try:
        await client.chat_update(
            channel=channel,
            ts=ts,
            text="Draft feedback recorded",
            blocks=feedback_resolved_blocks(message.get("blocks", []), user_id, action_id),
        )
    except SlackApiError as exc:
        log.warning("could not update card %s: %s", ts, exc.response.get("error"))


def register_feedback_handlers(app: AsyncApp, record: FeedbackRecorder) -> None:
    def make(action_id: str):
        async def handler(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
            await handle_feedback_click(action_id, ack, body, client, record)

        return handler

    for action_id in (GOOD_DRAFT, BAD_DRAFT):
        app.action(action_id)(make(action_id))
