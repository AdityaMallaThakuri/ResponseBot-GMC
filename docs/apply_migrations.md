# Applying the database migrations

The migrations in `db/migrations/` are plain Postgres (15+) and work on Supabase. Nothing applies
them automatically: you run them by hand, in numeric order. They create 13 tables, so apply them
to a scratch project first and check them before your real one.

| File | Creates |
|---|---|
| `0001_raw_events.sql` | `set_updated_at()` trigger function, `raw_events` |
| `0002_threads_messages.sql` | `threads`, `messages` |
| `0003_classifications_qualifications.sql` | `classifications`, `qualifications` |
| `0004_drafts_cards_actions.sql` | `drafts`, `slack_cards`, `actions` |
| `0005_suppressions.sql` | `suppressions` |
| `0006_retry_eval.sql` | `retry_queue`, `eval_results` |
| `0007_lock_down.sql` | revokes `anon` / `authenticated` table privileges |
| `0008_decisions_gold_labels.sql` | `decisions`, `gold_labels`; adds `classifications.extracted_facts`, `threads.vertical`, `drafts.template_version` |

Every file uses `create ... if not exists` or `add column if not exists`, so re-running one is
harmless. Never edit an applied file: to change a table later, add `0009_...sql`. A migration that
creates a table must also enable row level security and revoke `anon` / `authenticated` access
(0008 does both for its two tables); `scripts/check_schema.py` fails if it does not.

`0008` adds a CHECK to `drafts` that requires `template_version` whenever `template_id` is set. If
`drafts` already holds rows, backfill `template_version` first, or the migration will fail on them.

## 0. Before you start

1. Validate locally (no database needed):
   ```
   .venv/Scripts/python scripts/check_schema.py
   ```
   Expect `OK: 13 tables, every migration parses, RLS on, tables match CLAUDE.md`.
2. Use a scratch Supabase project (or a Supabase branch) for the first run.
3. Do not put the database password in `.env` or commit it. If you use `psql` (option B), keep the
   connection string in your shell only.

## Option A: Supabase SQL Editor (no tools to install)

For each file, in order `0001` to `0008`:

1. Open the project in the Supabase dashboard, then **SQL Editor** > **New query**.
2. Paste the whole file and press **Run**.
3. Confirm it says success before moving to the next file. If a file fails, stop, read the error,
   and fix the cause. Do not skip ahead: later files depend on earlier tables.

## Option B: psql

Get the connection string from the dashboard (**Connect** > **Session pooler** or **Direct
connection**) and set it in your shell for this session only:

```
export SUPABASE_DB_URL='postgresql://postgres:<password>@<host>:5432/postgres'
for f in db/migrations/*.sql; do
  echo "applying $f"
  psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f "$f" || break
done
```

`ON_ERROR_STOP=1` and `|| break` stop at the first failing file. The glob runs files in numeric
order because the numbers are zero-padded. In PowerShell, set `$env:SUPABASE_DB_URL` and loop with
`Get-ChildItem db\migrations\*.sql | Sort-Object Name`.

## Verify (run in the SQL Editor)

All nine checks should give the stated result.

**1. All 13 tables exist** (expect 13 rows):
```sql
select table_name from information_schema.tables
where table_schema = 'public' and table_name in (
  'raw_events','threads','messages','classifications','qualifications',
  'drafts','slack_cards','actions','suppressions','retry_queue','eval_results','decisions','gold_labels')
order by table_name;
```

**2. Row level security is on everywhere** (expect `true` in every row):
```sql
select c.relname, c.relrowsecurity
from pg_class c join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relkind = 'r' and c.relname in (
  'raw_events','threads','messages','classifications','qualifications',
  'drafts','slack_cards','actions','suppressions','retry_queue','eval_results','decisions','gold_labels')
order by c.relname;
```

**3. There are no policies** (expect `0`):
```sql
select count(*) from pg_policies where schemaname = 'public';
```

**4. `anon` and `authenticated` have no table privileges** (expect no rows):
```sql
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_schema = 'public' and grantee in ('anon', 'authenticated')
  and table_name in (
  'raw_events','threads','messages','classifications','qualifications',
  'drafts','slack_cards','actions','suppressions','retry_queue','eval_results','decisions','gold_labels');
```

**5. The message uniqueness key** (expect one row ending in
`UNIQUE (thread_id, direction, sent_at, message_id)`):
```sql
select conname, pg_get_constraintdef(oid) from pg_constraint
where conname = 'messages_thread_direction_sent_at_message_id_key';
```

**6. Behaviour smoke test.** This inserts a test thread and messages, checks that a SENT and a
REPLY sharing one `stats_id` are both kept and that an exact duplicate is rejected, then raises an
error on purpose so everything rolls back. Success looks like an error whose message is
`SMOKE TEST PASSED (rolled back on purpose, nothing was stored)`. Any other message is a failure.
```sql
do $$
declare tid uuid;
begin
  insert into threads (email_lead_id, email_campaign_id, lead_email)
    values (1, 1, 'smoke@example.com') returning id into tid;
  insert into messages (thread_id, direction, sent_at, stats_id, message_id,
                        from_address, to_address, body_clean)
    values (tid, 'SENT',  '2026-01-01T00:00:00Z', 'shared', '<a@x>', 'a@x', 'b@x', 'hi'),
           (tid, 'REPLY', '2026-01-01T00:00:00Z', 'shared', '<b@x>', 'b@x', 'a@x', 'yo');
  begin
    insert into messages (thread_id, direction, sent_at, stats_id, message_id,
                          from_address, to_address, body_clean)
      values (tid, 'SENT', '2026-01-01T00:00:00Z', 'shared', '<a@x>', 'a@x', 'b@x', 'hi');
    raise exception 'FAIL: duplicate message was accepted';
  exception when unique_violation then
    null;
  end;
  raise exception 'SMOKE TEST PASSED (rolled back on purpose, nothing was stored)';
end $$;
```

**7. The 0008 columns and constraints** (expect 3 column rows, all nullable `YES` because
`template_version` is enforced by a CHECK, then 2 constraint rows):
```sql
select table_name, column_name, data_type, is_nullable
from information_schema.columns
where table_schema = 'public'
  and (table_name, column_name) in (
    ('classifications', 'extracted_facts'), ('threads', 'vertical'), ('drafts', 'template_version'))
order by table_name, column_name;

select conname, pg_get_constraintdef(oid) from pg_constraint
where conname in ('drafts_template_version_needs_template', 'decisions_draft_id_key');
```

**8. Smoke test: one decision per draft.** This builds a thread, a reply message, a classification
and a draft, records one decision, then tries to record a second decision for the same draft. The
database must reject it with the unique constraint `decisions_draft_id_key` (any other rejection
counts as a failure). It then raises an error on purpose so everything rolls back. Run it on its
own; success is the same message as check 6.
```sql
do $$
declare
  tid uuid;
  mid uuid;
  cid uuid;
  did uuid;
  cname text;
begin
  insert into threads (email_lead_id, email_campaign_id, lead_email)
    values (2, 2, 'smoke2@example.com') returning id into tid;
  insert into messages (thread_id, direction, sent_at, stats_id, message_id,
                        from_address, to_address, body_clean)
    values (tid, 'REPLY', '2026-01-01T00:00:00Z', 'smoke', '<r@x>', 'a@x', 'b@x', 'yo')
    returning id into mid;
  insert into classifications (thread_id, reply_message_id, label, confidence, reason,
                               model, prompt_version)
    values (tid, mid, 'meeting_request', 0.9, 'smoke', 'smoke', 'smoke') returning id into cid;
  insert into drafts (thread_id, classification_id, template_id, template_version, text,
                      validator_ok)
    values (tid, cid, 'T1', 'smoke', 'hi', true) returning id into did;

  insert into decisions (draft_id, decided_by, decision, final_text)
    values (did, 'U012ABCDEF', 'approved', 'hi');

  begin
    insert into decisions (draft_id, decided_by, decision, skip_reason)
      values (did, 'U099ZZZZZZ', 'skipped', 'other');
    raise exception 'FAIL: a second decision for the same draft was accepted';
  exception when unique_violation then
    get stacked diagnostics cname = constraint_name;
    if cname is distinct from 'decisions_draft_id_key' then
      raise exception 'FAIL: rejected by %, expected decisions_draft_id_key', cname;
    end if;
  end;

  raise exception 'SMOKE TEST PASSED (rolled back on purpose, nothing was stored)';
end $$;
```

**9. Smoke test: suppression scope rules.** This checks that a global suppression without
`confirmed_by` is rejected by `global_scope_needs_human`, that a campaign-scope suppression without
`campaign_id` is rejected by `campaign_scope_needs_campaign`, and that the valid forms of both are
accepted (so the rejections are not caused by something else). It then raises an error on purpose
so everything rolls back. Run it on its own; success is the same message as check 6.
```sql
do $$
declare cname text;
begin
  begin
    insert into suppressions (email, scope, reason)
      values ('smoke@example.com', 'global', 'smoke');
    raise exception 'FAIL: a global suppression without confirmed_by was accepted';
  exception when check_violation then
    get stacked diagnostics cname = constraint_name;
    if cname is distinct from 'global_scope_needs_human' then
      raise exception 'FAIL: rejected by %, expected global_scope_needs_human', cname;
    end if;
  end;

  begin
    insert into suppressions (email, scope, reason)
      values ('smoke@example.com', 'campaign', 'smoke');
    raise exception 'FAIL: a campaign suppression without campaign_id was accepted';
  exception when check_violation then
    get stacked diagnostics cname = constraint_name;
    if cname is distinct from 'campaign_scope_needs_campaign' then
      raise exception 'FAIL: rejected by %, expected campaign_scope_needs_campaign', cname;
    end if;
  end;

  -- the valid forms must still work
  insert into suppressions (email, scope, reason, confirmed_by)
    values ('smoke@example.com', 'global', 'smoke', 'human:U012ABCDEF');
  insert into suppressions (email, scope, reason, campaign_id)
    values ('smoke@example.com', 'campaign', 'smoke', 1);

  raise exception 'SMOKE TEST PASSED (rolled back on purpose, nothing was stored)';
end $$;
```

## After applying

- The app reaches these tables only with the **service key** (`SUPABASE_SERVICE_KEY`), which
  bypasses row level security. The anon and authenticated keys can read and write nothing.
- Nothing records which files you applied. Note it somewhere, for example "applied 0001 to 0008 to
  <project> on <date>", so the next migration starts at the right number.

## Undoing a mistake

There are no down migrations. On a scratch project, the simplest reset is to drop the tables in
reverse dependency order:

```sql
drop table if exists gold_labels, decisions, eval_results, retry_queue, suppressions, actions,
  slack_cards, drafts, qualifications, classifications, messages, threads, raw_events cascade;
drop function if exists set_updated_at();
```

Do not run this on a project that holds real data.
