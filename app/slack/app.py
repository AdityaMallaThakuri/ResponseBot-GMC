from fastapi import APIRouter, Request, Response
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp

from app.config.settings import Settings
from app.slack.handlers import Recorder, register_card_handlers

router = APIRouter()


def create_slack_app(settings: Settings, record: Recorder | None = None) -> AsyncApp:
    app = AsyncApp(
        token=settings.SLACK_BOT_TOKEN,
        signing_secret=settings.SLACK_SIGNING_SECRET,
        # Bolt rejects any request whose X-Slack-Signature does not match (HTTP 401).
        request_verification_enabled=True,
    )
    register_card_handlers(app, record)
    return app


def create_slack_handler(
    settings: Settings, record: Recorder | None = None
) -> AsyncSlackRequestHandler:
    return AsyncSlackRequestHandler(create_slack_app(settings, record))


@router.post("/slack/events")
async def slack_events(request: Request) -> Response:
    return await request.app.state.slack_handler.handle(request)
