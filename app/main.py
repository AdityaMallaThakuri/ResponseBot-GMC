import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.settings import get_settings
from app.ingest.webhook import router as webhook_router
from app.slack.app import create_slack_handler
from app.slack.app import router as slack_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fail fast at startup if configuration is incomplete
    if not settings.gmc_campaign_ids:
        logging.getLogger(__name__).warning(
            "GMC_CAMPAIGN_IDS is empty: every Smartlead webhook will be ignored"
        )
    app.state.slack_handler = create_slack_handler(settings)
    yield


app = FastAPI(title="AI Reply Bot", lifespan=lifespan)
app.include_router(slack_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
