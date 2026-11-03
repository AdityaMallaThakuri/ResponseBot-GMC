"""Button handlers. For now a click only acknowledges and updates the message.

Nothing here touches the database. Never log a payload: it can carry email text.
"""

import logging
from typing import Any

from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.slack.card import APPROVE, CHOICES, EDIT, SKIP, resolved_blocks

log = logging.getLogger(__name__)


async def handle_card_click(
    action_id: str, ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient
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


async def on_approve(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
    await handle_card_click(APPROVE, ack, body, client)


async def on_edit(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
    await handle_card_click(EDIT, ack, body, client)


async def on_skip(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
    await handle_card_click(SKIP, ack, body, client)


def register_card_handlers(app: AsyncApp) -> None:
    app.action(APPROVE)(on_approve)
    app.action(EDIT)(on_edit)
    app.action(SKIP)(on_skip)
