"""Button handlers. A click acknowledges, updates the message and, if a recorder is given, records
the decision on the card (Approve and Skip only; see app/slack/decisions.py).

Nothing here sends, edits a draft or calls Smartlead. Never log a payload: it can carry email text.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi.concurrency import run_in_threadpool
from slack_bolt.async_app import AsyncAck, AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.slack.card import (
    APPROVE,
    CHOICES,
    EDIT,
    RETRY_SEND,
    SKIP,
    resolved_blocks,
    with_send_outcome,
)
from app.slack.service import EDIT_CALLBACK_ID, TEXT_ACTION_ID, TEXT_BLOCK_ID, CardService

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


async def handle_service_click(
    action_id: str,
    ack: AsyncAck,
    body: dict[str, Any],
    client: AsyncWebClient,
    service: CardService,
) -> None:
    """A click on a card (C3). Approve and Skip record a decision; Edit opens a window."""
    await ack()
    message = body.get("message") or {}
    channel = (body.get("channel") or {}).get("id")
    user_id = (body.get("user") or {}).get("id")
    ts = message.get("ts")
    if not (channel and user_id and ts):
        log.warning("card action %s arrived without a message, channel or user", action_id)
        return
    try:
        if action_id == EDIT:
            view = await run_in_threadpool(service.edit_view, channel, ts)
            if view is None:
                await client.chat_postEphemeral(
                    channel=channel, user=user_id, text="This draft cannot be edited."
                )
                return
            await client.views_open(trigger_id=body["trigger_id"], view=view)
            return
        result = await run_in_threadpool(service.click, channel, ts, action_id, user_id)
    except SlackApiError as exc:
        log.warning("card action %s failed in Slack: %s", action_id, exc.response.get("error"))
        return
    except Exception as exc:  # noqa: BLE001 - never log the payload
        log.warning("card action %s failed: %s", action_id, type(exc).__name__)
        return
    if result.kind == "blocked":
        await client.chat_postEphemeral(
            channel=channel, user=user_id, text=result.message or "Blocked."
        )
        return
    if result.kind not in ("recorded", "unknown"):
        return
    blocks = resolved_blocks(message.get("blocks", []), user_id, action_id)
    if result.send is not None:
        retry_ref = ts if result.send.retryable else None
        blocks = with_send_outcome(blocks, result.send.text, retry_ref)
    try:
        await client.chat_update(
            channel=channel,
            ts=ts,
            text=f"Reply card: {CHOICES[action_id][0]}",
            blocks=blocks,
        )
    except SlackApiError as exc:
        log.warning("could not update card %s: %s", ts, exc.response.get("error"))


async def handle_retry_send(
    ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient, service: CardService
) -> None:
    """The Retry send button: try the send again and show what happened."""
    await ack()
    message = body.get("message") or {}
    channel = (body.get("channel") or {}).get("id")
    user_id = (body.get("user") or {}).get("id")
    ts = message.get("ts")
    if not (channel and user_id and ts):
        return
    try:
        result = await run_in_threadpool(service.retry_send, channel, ts, user_id)
        if result.kind != "recorded" or result.send is None:
            return
        retry_ref = ts if result.send.retryable else None
        blocks = with_send_outcome(message.get("blocks", []), result.send.text, retry_ref)
        await client.chat_update(
            channel=channel, ts=ts, text=f"Reply card: {result.send.status}", blocks=blocks
        )
    except SlackApiError as exc:
        log.warning("could not update the card after a retry: %s", exc.response.get("error"))
    except Exception as exc:  # noqa: BLE001 - never log the payload
        log.warning("retry send failed: %s", type(exc).__name__)


async def handle_edit_submission(
    ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient, service: CardService
) -> None:
    """The Edit window was saved: re-check the text, store it and refresh the card."""
    view = body.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        text = view["state"]["values"][TEXT_BLOCK_ID][TEXT_ACTION_ID]["value"] or ""
    except (KeyError, ValueError):
        await ack()
        log.warning("edit submission arrived without its card or text")
        return
    if not text.strip():
        await ack(response_action="errors", errors={TEXT_BLOCK_ID: "The reply cannot be empty."})
        return
    await ack()
    try:
        result = await run_in_threadpool(service.submit_edit, meta["channel"], meta["ts"], text)
        if result is not None:
            await client.chat_update(
                channel=meta["channel"], ts=meta["ts"], text=result.text, blocks=result.blocks
            )
    except SlackApiError as exc:
        log.warning("could not refresh the edited card: %s", exc.response.get("error"))
    except Exception as exc:  # noqa: BLE001 - never log the payload
        log.warning("could not save the edit: %s", type(exc).__name__)


def register_card_handlers(
    app: AsyncApp, record: Recorder | None = None, service: CardService | None = None
) -> None:
    if service is not None:

        def make_service(action_id: str):
            async def handler(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
                await handle_service_click(action_id, ack, body, client, service)

            return handler

        for action_id in (APPROVE, EDIT, SKIP):
            app.action(action_id)(make_service(action_id))

        async def on_submit(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
            await handle_edit_submission(ack, body, client, service)

        app.view(EDIT_CALLBACK_ID)(on_submit)

        async def on_retry(ack: AsyncAck, body: dict[str, Any], client: AsyncWebClient) -> None:
            await handle_retry_send(ack, body, client, service)

        app.action(RETRY_SEND)(on_retry)
        return
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
