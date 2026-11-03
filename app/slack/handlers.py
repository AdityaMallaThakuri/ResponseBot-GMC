"""Button handlers. A click acknowledges, updates the message and, if a recorder is given, records
the decision on the card (Approve and Skip only; see app/slack/decisions.py).

Nothing here sends, edits a draft or calls Smartlead. Never log a payload: it can carry email text.
"""

import logging
from collections.abc import Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool
from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.slack.card import APPROVE, CHOICES, EDIT, SKIP, resolved_blocks

log = logging.getLogger(__name__)

# (channel id, message ts, action id) -> True if the click was recorded
Recorder = Callable[[str, str, str], bool]


async def handle_card_click(
    action_id: str,
    ack: AsyncAck,
    body: dict[str, Any],
    client: AsyncWebClient,
    record: Recorder | None = None,
) -> None:
    await ack()
    message = body.get("message")
    channel = (body.get("channel") or {}).get("id")
    user_id = (body.get("user") or {}).get("id")
    if not (message and channel and user_id and message.get("ts")):
        log.warning("card action %s arrived without a message, channel or user", action_id)
        return
    blocks = resolved_blocks(message.get("blocks", []), user_id, action_id)
    try:
        await client.chat_update(
            channel=channel,
            ts=message["ts"],
            text=f"Reply card: {CHOICES[action_id][0]}",
            blocks=blocks,
        )
    except SlackApiError as exc:
        log.warning("could not update card %s: %s", message["ts"], exc.response.get("error"))
        return
    log.info("card action %s by %s on %s", action_id, user_id, message["ts"])
    if record is not None:
        try:
            await run_in_threadpool(record, channel, message["ts"], action_id)
        except Exception as exc:  # noqa: BLE001 - the click already worked; never log the payload
            log.warning("could not record card action %s: %s", action_id, type(exc).__name__)


async def on_approve(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
    await handle_card_click(APPROVE, ack, body, client)


async def on_edit(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
    await handle_card_click(EDIT, ack, body, client)


async def on_skip(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
    await handle_card_click(SKIP, ack, body, client)


def register_card_handlers(app: AsyncApp, record: Recorder | None = None) -> None:
    if record is None:
        app.action(APPROVE)(on_approve)
        app.action(EDIT)(on_edit)
        app.action(SKIP)(on_skip)
        return

    def make(action_id: str):
        async def handler(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
            await handle_card_click(action_id, ack, body, client, record)

        return handler

    for action_id in (APPROVE, EDIT, SKIP):
        app.action(action_id)(make(action_id))
