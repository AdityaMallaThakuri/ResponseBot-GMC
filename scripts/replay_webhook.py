"""POST a saved webhook fixture to a running server, for testing with ngrok.

Usage:
    python scripts/replay_webhook.py
    python scripts/replay_webhook.py path/to/payload.json --url https://abc123.ngrok-free.app/webhooks/smartlead
    python scripts/replay_webhook.py --fresh        # new reply time, so it is not a duplicate
    python scripts/replay_webhook.py --check        # then read back what the pipeline stored
    python scripts/replay_webhook.py --check-only   # no POST: just read back what is stored

The secret is WEBHOOK_SECRET from .env (or --secret). It is never printed. By default the request
carries an X-Smartlead-Signature header (sha256 HMAC of the body); --auth header|body sends the
secret as X-Webhook-Secret or as a secret_key field instead.

--check waits for the background task, then reads (never writes) the database and prints the event
status, the latest classification (label, confidence, prompt version) and the latest Slack card
(lane, status, channel). No email text is printed. The pipeline classifies the first reply and
posts a card to Slack, so use a test channel. Otherwise only the status code and the server's JSON
answer are printed. Note: the server fetches the real
thread from Smartlead, so a replayed fixture only processes fully if its campaign_id is in
GMC_CAMPAIGN_IDS and the lead exists in that campaign. Otherwise the raw event is stored and the
failure is written on it. Nothing here sends, unsubscribes or writes to Smartlead.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "webhook_email_reply.json"
DEFAULT_URL = "http://127.0.0.1:8000/webhooks/smartlead"


def build_request(
    payload: dict[str, Any], secret: str, auth: str = "signature"
) -> tuple[bytes, dict[str, str]]:
    """The body and headers to send. The secret itself is only sent for auth header or body."""
    headers = {"Content-Type": "application/json"}
    if auth == "body":
        payload = {**payload, "secret_key": secret}
    body = json.dumps(payload).encode("utf-8")
    if auth == "signature":
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Smartlead-Signature"] = f"sha256={digest}"
    elif auth == "header":
        headers["X-Webhook-Secret"] = secret
    elif auth != "body":
        raise ValueError(f"unknown auth mode: {auth}")
    return body, headers


def replay(
    url: str,
    fixture: Path,
    secret: str,
    auth: str = "signature",
    fresh: bool = False,
    client: Any = None,
) -> httpx.Response:
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    if fresh:
        payload["time_replied"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body, headers = build_request(payload, secret, auth)
    if client is None:
        with httpx.Client(timeout=30.0) as http:
            return http.post(url, content=body, headers=headers)
    return client.post(url, content=body, headers=headers)


def latest(db: Any, table: str) -> dict[str, Any] | None:
    rows = db.table(table).select("*").order("created_at", desc=True).limit(1).execute().data
    return rows[0] if rows else None


def summarise_pipeline(db: Any) -> list[str]:
    """What the newest event, classification and card look like. Never includes email text."""
    lines = []
    event = latest(db, "raw_events")
    if event:
        error = event.get("process_error") or "none"
        lines.append(f"event: processed={event.get('processed')}, process_error={error}")
    else:
        lines.append("event: none stored")
    c = latest(db, "classifications")
    if c:
        lines.append(
            f"classification: {c['label']} at {c['confidence']} "
            f"(model {c['model']}, prompt version {c['prompt_version']}, rule {c.get('rule_hit')})"
        )
    else:
        lines.append("classification: none")
    card = latest(db, "slack_cards")
    if card:
        lines.append(
            f"slack card: lane {card['lane']}, status {card['status']}, channel {card['channel']}"
        )
    else:
        lines.append("slack card: none")
    action = latest(db, "actions")
    if action:
        lines.append(f"latest action: {action['type']} by {action['triggered_by']}")
    return lines


def check(wait: float = 20.0, db: Any = None, sleep: Any = time.sleep) -> list[str]:
    """Poll until the newest event is processed (or time runs out), then summarise."""
    if db is None:
        sys.path.insert(0, str(ROOT))
        from app.config.settings import get_settings
        from app.db.connect import create_db

        db = create_db(get_settings())
    waited = 0.0
    while True:
        event = latest(db, "raw_events")
        if (event and event.get("processed")) or waited >= wait:
            return summarise_pipeline(db)
        sleep(1.0)
        waited += 1.0


def load_secret(explicit: str | None) -> str:
    if explicit:
        return explicit
    from dotenv import dotenv_values

    secret = dotenv_values(ROOT / ".env").get("WEBHOOK_SECRET")
    if not secret:
        raise SystemExit("WEBHOOK_SECRET is not set in .env; pass --secret")
    return secret


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE)
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--auth", choices=["signature", "header", "body"], default="signature")
    p.add_argument("--secret", help="override WEBHOOK_SECRET from .env")
    p.add_argument("--fresh", action="store_true", help="use the current time as the reply time")
    p.add_argument("--check", action="store_true", help="afterwards, read back what was stored")
    p.add_argument("--check-only", action="store_true", help="send nothing, only read back")
    args = p.parse_args(argv)

    if args.check_only:
        print("\n".join(check(wait=0.0)))
        return 0
    try:
        response = replay(args.url, args.fixture, load_secret(args.secret), args.auth, args.fresh)
    except httpx.HTTPError as exc:
        print(f"Could not reach {args.url}: {type(exc).__name__}")
        return 1
    print(f"HTTP {response.status_code}")
    print(response.text[:500])
    if args.check and response.status_code == 200:
        print("\n".join(check()))
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
