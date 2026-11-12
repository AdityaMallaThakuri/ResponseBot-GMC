import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from app.config.settings import get_settings
from app.db.client import fetch_announced_campaign_ids, record_announced_campaign
from app.examples.learn import learn_once
from app.ingest.log_redact import install_redaction
from app.ingest.webhook import get_deps
from app.ingest.webhook import router as webhook_router
from app.slack.app import create_slack_handler
from app.slack.app import router as slack_router
from app.slack.card import escape_mrkdwn
from app.slack.feedback import record_feedback
from app.smartlead.watch import AttachJob

log = logging.getLogger(__name__)


async def learning_loop(app: FastAPI, minutes: int) -> None:
    """Every `minutes`, save the replies Ray sent as new examples. Read only; never raises."""
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            await run_in_threadpool(learn_once, get_deps(app))
        except Exception as exc:  # noqa: BLE001 - keep looping; the next run tries again
            log.error("learning loop run failed: %s", type(exc).__name__)


WEBHOOK_JOB_MINUTES = 60


def slack_note_poster(settings: Any, slack: Any) -> Callable[[str], None]:
    channel = settings.SLACK_ALERT_CHANNEL or settings.SLACK_DRAFT_CHANNEL

    def post(text: str) -> None:
        if not channel:
            log.info("no Slack channel set; note not posted")
            return
        slack.chat_postMessage(channel=channel, text=escape_mrkdwn(text))

    return post


def build_attach_job(app: FastAPI) -> AttachJob:
    settings = get_settings()
    deps = get_deps(app)
    db = deps.db
    return AttachJob(
        deps.smartlead,
        get_settings,
        slack_note_poster(settings, deps.slack),
        lambda kind: fetch_announced_campaign_ids(db, kind),
        lambda kind, cid, name: record_announced_campaign(db, kind, cid, name),
    )


async def webhook_job_loop(app: FastAPI) -> None:
    """Once at startup, then every 60 minutes: attach our webhook to newly watched campaigns."""
    while True:
        try:
            job = getattr(app.state, "attach_job", None)
            if job is None:
                job = build_attach_job(app)
                app.state.attach_job = job
            await run_in_threadpool(job.run_once)
        except Exception as exc:  # noqa: BLE001 - keep looping; the next run tries again
            log.error("webhook job run failed: %s", type(exc).__name__)
        await asyncio.sleep(WEBHOOK_JOB_MINUTES * 60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fail fast at startup if configuration is incomplete
    install_redaction()  # keep ?secret=... out of uvicorn's access log
    if settings.gmc_campaign_ids:
        log.warning("GMC_CAMPAIGN_IDS is set: only those campaigns are watched (testing override)")
    send_flag = settings.SEND_ENABLED or settings.LIVE_SEND_ALL
    if os.environ.get("RAILWAY_ENVIRONMENT") and send_flag:
        # Shadow mode: a deployed copy refuses to start if a send flag is on.
        raise RuntimeError("SEND_ENABLED and LIVE_SEND_ALL must be false in production")
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
    job_task = None
    if settings.PUBLIC_BASE_URL.strip():
        job_task = asyncio.create_task(webhook_job_loop(app))
    else:
        log.info("PUBLIC_BASE_URL is not set: the webhook attach job is off")
    yield
    for t in (task, job_task):
        if t is not None:
            t.cancel()
            with suppress(asyncio.CancelledError):
                await t


app = FastAPI(title="AI Reply Bot", lifespan=lifespan)
app.include_router(slack_router)
app.include_router(webhook_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
