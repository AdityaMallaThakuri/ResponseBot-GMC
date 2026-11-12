# Going live in shadow mode (Railway)

Shadow mode: the bot drafts, Ray copies from Slack and sends in Smartlead. The bot never sends.
`SEND_ENABLED=false` and `LIVE_SEND_ALL=false` stay false in production. A copy running on Railway
refuses to start if either is true.

## 1. Environment variables (Railway > service > Variables)

Secrets (paste the value, never commit or print it):

| Variable | Notes |
|---|---|
| `SMARTLEAD_API_KEY` | secret; the bot only reads with it |
| `ANTHROPIC_API_KEY` | secret |
| `SLACK_BOT_TOKEN` | secret |
| `SLACK_SIGNING_SECRET` | secret |
| `SUPABASE_URL` | not secret, but project-specific |
| `SUPABASE_SERVICE_KEY` | secret (service role key: full access) |
| `WEBHOOK_SECRET` | secret, 32+ characters: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

Settings (not secret):

| Variable | Value |
|---|---|
| `SEND_ENABLED` | `false` (set explicitly) |
| `LIVE_SEND_ALL` | `false` (set explicitly) |
| `CATEGORY_WRITEBACK_ENABLED` | `false` |
| `SEND_ALLOWLIST` | leave empty |
| `GMC_CAMPAIGN_IDS` | from `scripts/attach_webhooks.py` (step 4); empty means every reply is ignored |
| `SLACK_DRAFT_CHANNEL` | channel for draft cards (name or id) |
| `SLACK_ALERT_CHANNEL` | channel for notices (optional, falls back to the draft channel) |
| `LEARN_INTERVAL_MINUTES` | `30` (the learning loop runs inside the app; 0 turns it off) |
| `PRICE_SENTENCE` | the price sentence Aryan approves, used word for word. Empty: cards say "Price not confirmed" |
| `DRAFT_MODEL`, `CLASSIFIER_MODEL`, `DRAFT_MIN_CONFIDENCE`, `CLASSIFIER_MIN_CONFIDENCE*` | optional; defaults are fine |

`PORT` is set by Railway. Do not set `RAILWAY_ENVIRONMENT`; Railway sets it.

## 2. Create the Railway service

1. railway.com > New Project > Deploy from GitHub repo > pick this repo (push `main` first). Railway
   reads `railway.toml` and `Dockerfile`; the health check is `GET /health`.
2. Open the service > Variables and add everything in section 1.
3. Settings > Networking > Generate Domain. The result looks like `https://<name>.up.railway.app`.
4. Wait for the deploy to go green, then open `<url>/health`: it must show `{"status":"ok"}`.
5. Slack: in the Slack app settings, set Interactivity Request URL to `<url>/slack/events`
   (buttons: Good draft / Bad draft).
6. In the deploy logs expect: no `SEND_ENABLED` warning and, once the learning loop runs,
   `learning loop: ... checked`.

## 3. Database (the Supabase project the bot will use)

Start from `docs/apply_migrations.md`. If 0001 to 0008 are not applied there, apply them first, in
order. Then apply, in this order, in the Supabase SQL editor (paste each file, Run):

1. `db/migrations/0009_actions_would_suppress.sql`
2. `db/migrations/0010_drafts_model_validator.sql`
3. `db/migrations/0011_reply_examples_feedback.sql`

Confirm (SQL editor):

```sql
select count(*) from reply_examples;                      -- 0 before loading, no error
select column_name from information_schema.columns
 where table_name = 'drafts' and column_name in ('model','validator_result','feedback');  -- 3 rows
select pg_get_constraintdef(oid) from pg_constraint
 where conname = 'actions_type_check';                    -- contains 'would_suppress'
select relrowsecurity from pg_class where relname = 'reply_examples';  -- true
```

Load the example library from your machine (needs `eval/data`, which is local only). Point `.env`
at that project's `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` first:

```
.venv/Scripts/python scripts/load_examples.py --dry-run     # expect 579 examples
.venv/Scripts/python scripts/load_examples.py               # inserts; safe to re-run
```

Then `select count(*) from reply_examples;` should show 579.

## 4. Webhook coverage

Smartlead does support an account-level webhook (`association_type: user`, covers all campaigns).
Do not use it: Smartlead's docs say it overrides every client and campaign webhook, so it could
silently break any other integration on the account. Use per-campaign webhooks instead:

```
.venv/Scripts/python scripts/attach_webhooks.py --base-url https://<name>.up.railway.app
```

This is read only: it lists GMC campaigns (POS REPLY ones are tagged), which already have our
webhook, and prints `GMC_CAMPAIGN_IDS=...`. Check the list. To attach (needs `WEBHOOK_SECRET` in
your local `.env`, the same value as in Railway):

```
.venv/Scripts/python scripts/attach_webhooks.py --base-url https://<name>.up.railway.app --apply
```

It asks you to type the count, creates only an `EMAIL_REPLY` webhook with `?secret=`, skips
campaigns that already have ours and never updates or deletes anything. Paste the printed
`GMC_CAMPAIGN_IDS` into Railway. The list and create routes are from Smartlead's reference and
unverified on live data; if the dry run fails, nothing was changed.
