# AI Reply Bot (Gamic Media)

## What this is
A service that reads every reply to Gamic Media's cold-email campaigns (Smartlead), decides what the reply is and how well the lead fits, then either drafts the playbook response for Ray to approve in Slack, takes a safe automatic action, or hands the thread to a human. We start with Gamic's own campaigns (names starting "GMC" in Smartlead) before any client.

## Pipeline
Smartlead webhook -> ingest and dedupe -> rules pre-filter -> LLM classifier -> qualifier -> router -> one of three lanes:
1. Draft for approval: template engine -> validator -> Slack card (Approve / Edit / Skip) -> send through Smartlead.
2. Automatic safe action: unsubscribe, pause, or ignore. Always logged and reversible.
3. Human only: Slack alert, no draft.
Every step is logged to Supabase.

## Stack
Python 3.12, FastAPI, Slack Bolt, Supabase Postgres, Anthropic API (small model for classification, mid-size model for the one vertical-specific line), Smartlead API, Google Calendar API, pytest, Docker, Railway. No n8n.

## Hard rules
- Never send an email to a real lead unless the config flag SEND_ENABLED is true. Default is false.
- The template engine may only fill these slots: NAME, DAYTIMEORDAYTIME (two real CST slots from the calendar), links, and one vertical-specific "AI triggers" line. The validator rejects any draft that changes text outside those slots, states a price figure, or makes a claim that is not in the playbook.
- Never invent a price, a case-study number, a time slot or a link. Use only what is in playbook.yaml and the calendar.
- Anything uncertain (low classifier confidence, Smartlead category disagrees, off-script message, validator failure) goes to a human with no draft.
- Do-not-contact and bounce replies are suppressed automatically and never drafted.
- Unsubscribe and not-interested replies are suppressed at campaign scope. A global unsubscribe happens only for explicit broad opt-outs (remove me from all lists, do not contact me again, legal threat, spam complaint), and only after a human confirms in Slack. The Smartlead client never goes global on its own. Suppressions record a scope (campaign, client, global).
- Secrets only in .env. Never log full email bodies at INFO level outside Supabase.
- Autonomy levels: notify only, draft for approval, automatic safe action. Auto-send is out of scope until the GMC pilot proves it.

## Labels
The label list lives in app/config/labels.yaml and is finalised in Phase B. Initial set: meeting_request, info_request, free_list_request, price_question, price_and_info, lead_magnet_request, trial_request, booked_confirmation, objection_info, objection_price, not_interested, wrong_person, out_of_office, auto_reply_or_bounce, do_not_contact, off_script.

## Playbook facts
Offer: fractional deal origination for M&A firms. Calendar link: https://calendly.com/gamicmedia/one-one. Book in CST. Templates T1 to T8 and the objection handlers live in app/config/playbook.yaml, copied word for word from Gamic's M&A GMC reply playbook.

## Smartlead facts
Rate limit 120 requests per minute per key, handle 429 with backoff. Master-inbox fetch: campaign ids max 5, limit max 20, page with offset. Thread text needs the message-history call. Smartlead category tags are a hint only; the bot classifies for itself.
- Inbox response shape (verified on real data): `{"ok": true, "data": [items], "offset", "limit"}`. Each item has `email_lead_id` and `email_lead_map_id` (strings), `email_campaign_id` (int), `lead_email`, and its history under `email_history`. The message-history route returns the same entries under `history`.
- Each history entry has `type` (`SENT` or `REPLY`), `time` (ISO), `email_body` (HTML, must be cleaned), and `stats_id`. The parser raises on any other shape.
- `reply_to_stats_id` is the `stats_id` of the lead's latest REPLY. This is unverified until the first approved staging send.
- Category write-back is off (`CATEGORY_WRITEBACK_ENABLED=false`, and it also needs `SEND_ENABLED`). It is limited to `reply_label` categories in `app/config/smartlead_categories.yaml`; workflow triggers ("Generate ..." and Find Phone Number) and unknown categories are never written. The per-lead category endpoint (method and path) is unverified and not implemented.

## Data model
Supabase Postgres. SQL lives in db/migrations (numbered, applied in order by hand, see docs/apply_migrations.md); `python scripts/check_schema.py` validates them against this section.
Tables: raw_events, threads, messages, classifications, qualifications, drafts, slack_cards, actions, suppressions, retry_queue, eval_results, decisions, gold_labels.
- Every table has a uuid primary key and created_at. Row level security is on with no policies: only the service key reads or writes.
- `stats_id` is NOT unique per message (a SENT and its REPLY can share one). `messages` is unique on (thread_id, direction, sent_at, message_id), so a replayed webhook never duplicates or drops a message.
- `threads` is unique on (email_lead_id, email_campaign_id). `raw_events` is unique on (source, payload_hash).
- A draft that failed the validator can only be skipped or failed, never approved or sent.
- `decisions` holds the one human decision per draft (unique on draft_id, so the first Slack click wins): approved, edited or skipped, with skip_reason, final_text, edit_distance_pct and the sent message. `gold_labels` holds human labels (message type, verdict, template T1 to T8) per thread and labeler, for the eval replay.
- `drafts.template_version` records which version of playbook.yaml produced a draft. `classifications.extracted_facts` holds structured facts from a reply (out-of-office return date, referred contact). `threads.vertical` holds the lead's vertical.
- Decision: no templates table and no events table. Templates stay in app/config/playbook.yaml, and git is their version history; raw_events already records every incoming payload.
- Every new table's migration must also enable RLS and revoke anon and authenticated access; `scripts/check_schema.py` fails otherwise.
- Suppressions keep scope (campaign, client, global) with CHECK constraints; global needs confirmed_by.

## Code layout
app/ (main.py, config/, ingest/, classify/, qualify/, templates/, validate/, slack/, smartlead/, db/), db/migrations (SQL), docs/, eval/ (gold set and replay script), tests/, scripts/, Dockerfile, README.md.

## Working agreements
- Show a plan before writing code. Keep changes small and focused on the current step.
- Write pytest tests in the same step as the code. Run them and report the result.
- Type hints everywhere, small functions, no secrets in code, clear errors.
- When a decision changes, update this file.