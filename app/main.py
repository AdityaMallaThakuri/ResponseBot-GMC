import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config.settings import get_settings
from app.ingest.webhook import get_deps
from app.ingest.webhook import router as webhook_router
from app.send.sender import SendConfig, SendService
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
    send_config = SendConfig.from_settings(settings)
    log = logging.getLogger(__name__)
    if settings.LIVE_SEND_ALL:
        log.warning(
            "LIVE_SEND_ALL is ON: the allowlist is bypassed. It must stay off until go-live"
        )
    elif settings.SEND_ENABLED and not send_config.allowlist:
        log.warning("SEND_ENABLED is on but SEND_ALLOWLIST is empty: nothing can be sent")
    elif settings.SEND_ENABLED:
        log.warning(
            "SEND_ENABLED is on: replies go only to %d allowlisted address(es)",
            len(send_config.allowlist),
        )
    sender = SendService(
        lambda: get_deps(app).db, lambda: get_deps(app).smartlead, playbook, facts, send_config
    )
    service = CardService(lambda: get_deps(app).db, playbook, facts, sender)
    app.state.slack_handler = create_slack_handler(settings, service=service)
    yield


app = FastAPI(title="AI Reply Bot", lifespan=lifespan)
app.include_router(slack_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
