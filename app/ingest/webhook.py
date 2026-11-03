"""POST /webhooks/smartlead: verify, filter, store the raw event, then process in the background.

Nothing in this step sends, unsubscribes, classifies or posts to Slack.

Order of checks, cheapest first:
1. Authenticate with WEBHOOK_SECRET. Bad or missing: 401, and nothing from the body is logged.
2. Only EMAIL_REPLY events. Anything else: 200 and a log line, not stored.
3. Only campaigns in GMC_CAMPAIGN_IDS. Anything else: 200 and a log line, not stored.
4. Store the raw payload (without any secret_key) in raw_events. A repeat delivery is a 200
   that stores and processes nothing.
5. Answer 200 and process in the background.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.config.settings import get_settings
from app.db.client import insert_raw_event, record_event_error
from app.db.records import NewRawEvent, RawSource, payload_hash
from app.ingest.deps import IngestDeps, build_deps
from app.ingest.process import process_reply_event

logger = logging.getLogger(__name__)
router = APIRouter()

HANDLED_EVENT_TYPES = frozenset({"EMAIL_REPLY"})
SIGNATURE_HEADER = "x-smartlead-signature"  # "sha256=<hex hmac of the raw body>"
SECRET_HEADER = "x-webhook-secret"  # the secret itself, for senders that can set a header
SECRET_BODY_FIELD = "secret_key"  # the secret itself, as a field in the JSON body

# Fields that identify one reply event, in the order they are tried. Smartlead's documentation
# for EMAIL_REPLY is inconsistent between pages and no real delivery has been captured yet, so
# each part accepts any of its known spellings.
_LEAD_FIELDS = ("sl_email_lead_id", "lead_id", "email_lead_id", "to_email")
_MESSAGE_FIELDS = ("reply_message.message_id", "stats_id", "message_id")
_TIME_FIELDS = ("reply_message.time", "event_timestamp", "time_replied")


def _dig(payload: Mapping[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _first(payload: Mapping[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    for name in names:
        value = _dig(payload, name)
        if value not in (None, "", {}):
            return name, value
    return None


def event_key(payload: Mapping[str, Any]) -> tuple[str, list[str]]:
    """(sha256 hex, names of the fields used) identifying one reply event.

    Built from event_type, campaign_id, the lead (id, or email lower-cased), and the reply's
    message id or stats id and its time. The same event delivered twice gives the same key even
    if the body differs in unrelated ways. If the payload lacks the fields to identify an event,
    the key falls back to a hash of the whole payload so the event is still stored.
    """
    parts: list[tuple[str, Any]] = []
    for name in ("event_type", "campaign_id"):
        if payload.get(name) not in (None, ""):
            parts.append((name, payload[name]))
    lead = _first(payload, _LEAD_FIELDS)
    message = _first(payload, _MESSAGE_FIELDS)
    when = _first(payload, _TIME_FIELDS)
    if len(parts) == 2 and lead and (message or when):
        parts += [lead, *(p for p in (message, when) if p)]
        text = "|".join(
            f"{name}={str(value).strip().lower() if name == 'to_email' else value}"
            for name, value in parts
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), [name for name, _ in parts]
    return payload_hash(dict(payload)), ["whole payload"]


def _parse_json(body: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_authentic(
    body: bytes, headers: Mapping[str, str], payload: dict[str, Any] | None, secret: str
) -> bool:
    """True when the request proves knowledge of the secret. Constant-time comparisons.

    The first of these that is present must be valid: the HMAC signature header, the secret
    header, the secret_key body field. Which one Smartlead sends is not yet confirmed.
    """
    signature = headers.get(SIGNATURE_HEADER)
    if signature:
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        given = signature.strip()
        return _equal(given, f"sha256={expected}") or _equal(given, expected)
    header_secret = headers.get(SECRET_HEADER)
    if header_secret:
        return _equal(header_secret.strip(), secret)
    body_secret = (payload or {}).get(SECRET_BODY_FIELD)
    if isinstance(body_secret, str) and body_secret:
        return _equal(body_secret, secret)
    return False


def get_deps(app: FastAPI) -> IngestDeps:
    deps = getattr(app.state, "ingest", None)
    if deps is None:
        deps = build_deps(get_settings())
        app.state.ingest = deps
    return deps


def run_processing(deps: IngestDeps, event_id: str, payload: dict[str, Any]) -> None:
    """Background task. Never raises: an unexpected error is written on the event instead."""
    try:
        process_reply_event(deps, event_id, payload)
    except Exception as exc:  # noqa: BLE001 - last resort, keep the event for a retry
        logger.error("event %s processing crashed: %s", event_id, type(exc).__name__)
        try:
            record_event_error(deps.db, event_id, f"processing crashed: {type(exc).__name__}")
        except Exception:  # noqa: BLE001
            logger.error("event %s: could not record the failure either", event_id)


def _reply(status: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"status": status, **extra})


@router.post("/webhooks/smartlead")
async def smartlead_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
    settings = get_settings()
    body = await request.body()
    payload = _parse_json(body)

    if not is_authentic(body, request.headers, payload, settings.WEBHOOK_SECRET):
        logger.warning("smartlead webhook rejected: bad or missing secret")
        raise HTTPException(status_code=401, detail="unauthorized")
    if payload is None:
        logger.warning("smartlead webhook rejected: body is not a JSON object")
        return JSONResponse({"status": "invalid"}, status_code=400)

    event_type = payload.get("event_type")
    if event_type not in HANDLED_EVENT_TYPES:
        logger.info("smartlead webhook ignored: event_type=%s", str(event_type)[:40])
        return _reply("ignored", reason="event_type")

    campaign_id = payload.get("campaign_id")
    try:
        campaign = int(campaign_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        campaign = None
    if campaign is None or campaign not in settings.gmc_campaign_ids:
        logger.info("smartlead webhook ignored: campaign_id=%s not in GMC_CAMPAIGN_IDS", campaign)
        return _reply("ignored", reason="campaign")

    stored = {k: v for k, v in payload.items() if k != SECRET_BODY_FIELD}
    key, fields = event_key(stored)
    deps = get_deps(request.app)
    try:
        result = await run_in_threadpool(
            insert_raw_event, deps.db, NewRawEvent(RawSource.SMARTLEAD_WEBHOOK, stored, key)
        )
    except Exception as exc:  # noqa: BLE001 - tell Smartlead to retry; never log the body
        logger.error("smartlead webhook: could not store the event: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="could not store the event") from None

    if not result.is_new:
        logger.info("smartlead webhook duplicate delivery of event %s", result.id)
        return _reply("duplicate")

    logger.info("smartlead webhook stored event %s (key fields: %s)", result.id, ",".join(fields))
    background.add_task(run_processing, deps, result.id, stored)
    return _reply("accepted")
