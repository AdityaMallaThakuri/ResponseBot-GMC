import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from app.config.settings import get_settings
from app.examples.learn import learn_once
from app.ingest.log_redact import install_redaction
from app.ingest.webhook import get_deps
from app.ingest.webhook import router as webhook_router
from app.slack.app import create_slack_handler
from app.slack.app import router as slack_router
from app.slack.feedback import record_feedback

log = logging.getLogger(__name__)


async def learning_loop(app: FastAPI, minutes: int) -> None:
    """Every `minutes`, save the replies Ray sent as new examples. Read only; never raises."""
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            await run_in_threadpool(learn_once, get_deps(app))
        except Exception as exc:  # noqa: BLE001 - keep looping; the next run tries again
            log.error("learning loop run failed: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fail fast at startup if configuration is incomplete
    install_redaction()  # keep ?secret=... out of uvicorn's access log
    if not settings.gmc_campaign_ids:
        log.warning("GMC_CAMPAIGN_IDS is empty: every Smartlead webhook will be ignored")
    if settings.SEND_ENABLED:
        log.warning(
            "SEND_ENABLED is on, but the bot no longer sends: a person sends from Smartlead"
        )

    def feedback(channel: str, ts: str, action_id: str, user_id: str) -> bool:
        return record_feedback(get_deps(app).db, channel, ts, action_id, user_id)

    app.state.slack_handler = create_slack_handler(settings, feedback=feedback)
    task = None
    if settings.LEARN_INTERVAL_MINUTES > 0:
        task = asyncio.create_task(learning_loop(app, settings.LEARN_INTERVAL_MINUTES))
    yield
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="AI Reply Bot", lifespan=lifespan)
app.include_router(slack_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
