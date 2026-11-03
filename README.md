# AI Reply Bot (Gamic Media)

Reads replies to Gamic Media's Smartlead cold-email campaigns, classifies and qualifies them,
and drafts, auto-handles or escalates. See `CLAUDE.md` for the full design and hard rules.

## Run locally

Requires Python 3.12.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env             # then fill in every value
uvicorn app.main:app --reload
```

The app refuses to start if any required variable is missing and lists which ones.
`SEND_ENABLED` defaults to `false`; nothing is sent to real leads unless it is `true`.

Check it: `curl http://localhost:8000/health` returns `{"status":"ok"}`.

## Test and lint

```bash
pytest
ruff check .
```

## Slack

The app serves Slack events and button clicks at `POST /slack/events`. Slack must be able to reach
that URL over public HTTPS, so for local runs start a tunnel to port 8000 (for example
`ngrok http 8000`) and set the Slack app's Interactivity Request URL to
`https://<tunnel-host>/slack/events`.

Post a sample card (the buttons only acknowledge the click for now):

```bash
python scripts/post_test_card.py reply-bot-test
```

## Docker

```bash
docker build -t response-bot .
docker run --env-file .env -p 8000:8000 response-bot
```
