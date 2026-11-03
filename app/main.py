import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.settings import get_settings
from app.ingest.webhook import get_deps
from app.ingest.webhook import router as webhook_router
from app.slack.app import create_slack_handler
from app.slack.app import router as slack_router
from app.slack.service import CardService
from app.templates.facts import load_facts
from app.templates.playbook import load_playbook


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fail fast at startup if configuration is incomplete
    if not settings.gmc_campaign_ids:
        logging.getLogger(__name__).warning(
            "GMC_CAMPAIGN_IDS is empty: every Smartlead webhook will be ignored"
        )

    facts = load_facts()
    playbook = load_playbook(facts=facts)  # fails fast if playbook.yaml is malformed
    if not playbook.templates:
        logging.getLogger(__name__).warning(
            "playbook.yaml has no templates: every reply goes to a human with no draft"
        )
    service = CardService(lambda: get_deps(app).db, playbook, facts)
    app.state.slack_handler = create_slack_handler(settings, service=service)
    yield


app = FastAPI(title="AI Reply Bot", lifespan=lifespan)
app.include_router(slack_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
