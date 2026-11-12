from fastapi import APIRouter, Request, Response
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler
from slack_bolt.async_app import AsyncApp

from app.config.settings import Settings
from app.slack.feedback import FeedbackRecorder, register_feedback_handlers
from app.slack.handlers import Recorder, register_card_handlers
from app.slack.service import CardService

router = APIRouter()


def create_slack_app(
    settings: Settings,
    record: Recorder | None = None,
    service: CardService | None = None,
    feedback: FeedbackRecorder | None = None,
) -> AsyncApp:
    app = AsyncApp(
        token=settings.SLACK_BOT_TOKEN,
        signing_secret=settings.SLACK_SIGNING_SECRET,
        # Bolt rejects any request whose X-Slack-Signature does not match (HTTP 401).
        request_verification_enabled=True,
    )
    if feedback is None or record is not None or service is not None:
        register_card_handlers(app, record, service)  # the Approve / Edit / Skip card (unused)
    if feedback is not None:
        register_feedback_handlers(app, feedback)
    return app


def create_slack_handler(
    settings: Settings,
    record: Recorder | None = None,
    service: CardService | None = None,
    feedback: FeedbackRecorder | None = None,
) -> AsyncSlackRequestHandler:
    return AsyncSlackRequestHandler(create_slack_app(settings, record, service, feedback))


@router.post("/slack/events")
async def slack_events(request: Request) -> Response:
    return await request.app.state.slack_handler.handle(request)
