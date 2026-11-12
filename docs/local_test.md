# Local test plan (Windows, PowerShell)

Goal: run the bot on your laptop behind your fixed ngrok domain, see a DRAFT card in Slack from the
saved lender reply, then test one real campaign with a real reply. The bot sends nothing:
`SEND_ENABLED=false` and `LIVE_SEND_ALL=false` the whole time. Use your Slack TEST channel.

Replace `<DOMAIN>` with your ngrok domain (for example `yourname.ngrok-free.app`). Run every
command from the repo folder `D:\GTM\GAMIC\ResponseBot-GMC`. Use two PowerShell windows (A and B).

## 0. `.env` for the local run

In `.env` set (keep the rest as it is):

```
SEND_ENABLED=false
LIVE_SEND_ALL=false
SLACK_DRAFT_CHANNEL=<your test channel>
SLACK_ALERT_CHANNEL=<your test channel>
GMC_CAMPAIGN_IDS=4043678
PUBLIC_BASE_URL=
AUTO_ATTACH_WEBHOOKS=false
LEARN_INTERVAL_MINUTES=30
```

`4043678` is the campaign in the saved lender reply ("GMC - LENDERS 29/09"). Leaving `PUBLIC_BASE_URL`
empty keeps the webhook job off for now. `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` must be those of
the project you want to test against.

## a) Start the server and ngrok

Window A (server):

```
.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Window B (tunnel). Use whichever form your ngrok version accepts:

```
ngrok http --url=<DOMAIN> 8000
ngrok http --domain=<DOMAIN> 8000
```

Check: `Invoke-RestMethod https://<DOMAIN>/health` prints `status ok`. In the Slack app settings set
Interactivity Request URL to `https://<DOMAIN>/slack/events` (Good draft / Bad draft buttons).

## b) Migrations 0009 to 0012 and the example library

Apply the files in the Supabase SQL editor, in this order (0001 to 0008 first if that project lacks
them; see `docs/apply_migrations.md`):

1. `db/migrations/0009_actions_would_suppress.sql`
2. `db/migrations/0010_drafts_model_validator.sql`
3. `db/migrations/0011_reply_examples_feedback.sql`
4. `db/migrations/0012_announced_campaigns.sql`

Check: `select count(*) from reply_examples;` returns 0 with no error. Then in window B or a third
window:

```
.venv\Scripts\python scripts\load_examples.py --dry-run
.venv\Scripts\python scripts\load_examples.py
```

Expect 579 in the dry run, and `select count(*) from reply_examples;` returns 579 afterwards.

## c) Replay the saved lender reply end to end

The server (window A) must be running. The server fetches the real thread from Smartlead, so the
lead in the fixture must exist in campaign 4043678. Sequence:

```
.venv\Scripts\python scripts\replay_webhook.py tests\fixtures\webhook_lender_reply.json --url http://localhost:8000/webhooks/smartlead --fresh --check
```

Name the lender fixture: without it the script sends a different default fixture, which the server
ignores. If the lead's thread already has a card from an earlier test, the replay is stored but no
new card is posted (a thread is classified and posted once). Remove only that old test card (get its
id with `select id, lane, created_at from slack_cards order by created_at desc limit 5;`), dry run
first, then re-post the stored thread with `reclassify_stored.py` below:

```
.venv\Scripts\python scripts\remove_test_card.py --card-id <CARD UUID>
.venv\Scripts\python scripts\remove_test_card.py --card-id <CARD UUID> --apply
```

(`--fresh` uses a new reply time so it is not a duplicate. `--check` waits, then reads back the
event status, label, confidence and Slack card lane. It prints no email text.)

Expect in Slack: a card in your test channel with a label, the lead's reply, a "Draft reply" code
block (the copy button is at its top right) and Good draft / Bad draft buttons. Click one: the
buttons turn into "who said what".

If the event was stored but no card appeared (for example Slack was down, or the migrations were not
applied yet), find the thread and re-process it. Dry run first:

```
.venv\Scripts\python scripts\replay_webhook.py --check-only
```
In the Supabase SQL editor: `select id, email_campaign_id, created_at from threads order by created_at desc limit 5;`
```
.venv\Scripts\python scripts\reclassify_stored.py --thread-id <THREAD UUID> --dry-run
.venv\Scripts\python scripts\reclassify_stored.py --thread-id <THREAD UUID>
```

`reclassify_stored.py` never calls Smartlead; it classifies the stored thread and posts the card
(it does not add anything if the thread already has one). If the card shows a label but no draft, the
example library is empty or the label's confidence was low: check step b.

## d) Live test on ONE active GMC campaign

1. Pick the campaign. List the campaigns the rule finds (read only, nothing is changed):

   ```
   .venv\Scripts\python scripts\attach_webhooks.py --base-url https://<DOMAIN>
   ```

   (With `GMC_CAMPAIGN_IDS=4043678` still set it shows only that one. To browse, blank
   `GMC_CAMPAIGN_IDS` for this one command, read the list, then set it to the id you choose.)
2. In `.env` set:

   ```
   GMC_CAMPAIGN_IDS=<ONE CAMPAIGN ID>
   PUBLIC_BASE_URL=https://<DOMAIN>
   AUTO_ATTACH_WEBHOOKS=true
   ```

   Only that campaign is watched, so the job can attach to nothing else.
3. Stop the server in window A (Ctrl+C) and start it again. It runs the webhook job once at
   startup. In the log expect `webhook job: 1 watched, 1 without our webhook, auto attach on` then
   `webhook job: attached to <name>`, and ONE Slack summary (the first-ever run posts only a total: "Watching 1 campaign."; later runs add "New: <names>" only when something is new). The job also runs
   every 60 minutes while the server is up; it will skip a campaign that already has our webhook.
4. Check in Smartlead (campaign settings, webhooks) that one `Reply Bot (EMAIL_REPLY)` webhook
   exists. Or re-run `attach_webhooks.py --base-url https://<DOMAIN>`: it now says "webhook already
   attached".
5. Wait for a real reply to that campaign. Keep the server and ngrok running. Expect the card in
   Slack as in step c. The learning loop also reads that thread (read only) every 30 minutes once
   Ray has replied from Smartlead, and saves his reply as a new example.

A real lead's reply is processed for real (classified, drafted, posted to Slack). Nothing is sent.

## e) Remove the ngrok webhook afterwards

Do this BEFORE deploying, so the campaign never points at your laptop. First stop the server (Ctrl+C
in window A), then:

```
.venv\Scripts\python scripts\attach_webhooks.py --detach-ours --campaign-id <ONE CAMPAIGN ID> --base-url https://<DOMAIN>
```

That is a dry run: it lists the webhooks whose URL is `https://<DOMAIN>/webhooks/smartlead` on that
campaign and removes nothing. If the list is right:

```
.venv\Scripts\python scripts\attach_webhooks.py --detach-ours --campaign-id <ONE CAMPAIGN ID> --base-url https://<DOMAIN> --apply
```

It asks you to type the count, then deletes only those webhooks by their own id. Any other webhook
on the campaign is left alone. Check in Smartlead (campaign settings, webhooks) that the ngrok one is
gone. The delete route is unverified (Smartlead's docs conflict; the script tries both forms): if it
fails, delete that one webhook in the Smartlead UI.

Then, in `.env`, undo the test settings so a later local start does not attach again:

```
GMC_CAMPAIGN_IDS=
PUBLIC_BASE_URL=
AUTO_ATTACH_WEBHOOKS=false
```

For the deployed app set `PUBLIC_BASE_URL` to the Railway domain in Railway, see
`docs/deploy_railway.md`.
