# A safe end-to-end send test (your own address only)

Goal: send one real reply through Smartlead to **your own email address** and nothing else. Three
locks protect you: `SEND_ENABLED`, `SEND_ALLOWLIST` (only your address) and `LIVE_SEND_ALL=false`.
Do not use a real lead. Do not set `LIVE_SEND_ALL=true`.

## 0. Before you start

- Migrations `0009` and `0010` are applied to the scratch Supabase project.
- You have a test campaign in Smartlead (not a live GMC campaign) whose only lead is **your own
  address**, sent from a connected mailbox. Note its campaign id.
- The Smartlead webhook for that campaign points at your ngrok URL (`/webhooks/smartlead`).

## 1. Put one test template in `app/config/playbook.yaml` (do not commit it)

Wording you write yourself, with no numbers or links, so no claim needs approving. Example shape:

```yaml
version: "test-1"
templates:
  T2:
    labels: [info_request]
    when: The lead asks how it works.
    text: |
      Hi {NAME}, this is a test reply from the reply bot. Please ignore it.
    slots:
      NAME: {source: first_name}
    max_chars: 300
```

Use a reply to your test email that the classifier calls `info_request` (for example "Sounds
interesting, can you tell me more about how this works?") and a draft confidence of 0.75 or more.

## 2. `.env` for the test

```
SEND_ENABLED=true
SEND_ALLOWLIST=you@your-address.com
LIVE_SEND_ALL=false
GMC_CAMPAIGN_IDS=<the test campaign id only>
SLACK_DRAFT_CHANNEL=reply-bot-test
```

Restart the server (`--reload` does not watch `.env`). The startup log says
`SEND_ENABLED is on: replies go only to 1 allowlisted address(es)`. If it says `LIVE_SEND_ALL is ON`,
stop and fix `.env`.

## 3. Check which message the reply will answer (read only)

Reply to your test email from your own address, wait for the Slack card, then:

```
.venv\Scripts\python scripts\check_reply_target.py --thread-id <thread uuid>
```

It lists the thread (ids only, the address is masked) and marks the message `reply_to_stats_id` points
to. Open the thread in Smartlead's inbox and confirm that message is the one **you** wrote. This is the
unverified assumption: if it is wrong, do not send.

## 4. Send

Click **Approve** on the card. The card changes to `Sent to y***@…` (or says why not). Check your
inbox, and in Supabase:

```sql
select type, triggered_by, details from actions where type = 'send' order by created_at desc limit 5;
```

`details.result` is `sent`, `failed` or `blocked_...`. The body is never stored. Things to look at in
your inbox: that it threads under your original message, and that the line breaks look right (the bot
sends simple HTML: one paragraph per blank line; whether Smartlead wants HTML there is unverified).

## 5. Prove the lock works

Change `SEND_ALLOWLIST` to a different address, restart, reply again and Approve: the card must say
`Not sent: the recipient is not in SEND_ALLOWLIST` and nothing arrives.

## 6. Switch it off again

Set `SEND_ENABLED=false`, empty `SEND_ALLOWLIST`, restore `playbook.yaml` (`git checkout
app/config/playbook.yaml`) and restart.
