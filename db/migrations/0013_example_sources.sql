-- 0013: two more example sources and two columns, for the Smartlead sync and the SOP templates.
--   source 'smartlead_sync'  pairs read from Smartlead by scripts/sync_examples.py (read only)
--   source 'sop'             reply templates from Ray's SOP file (scripts/import_sop.py)
--   sent_at   when Gamic's reply was sent (the date the library ranks recency by)
--   priority  higher wins within a label (SOP templates are 100, everything else 0)
-- Apply after 0011. Safe to re-run.

alter table reply_examples drop constraint if exists reply_examples_source_check;
alter table reply_examples add constraint reply_examples_source_check
    check (source in ('history', 'ray_sent', 'smartlead_sync', 'sop'));
alter table reply_examples add column if not exists sent_at timestamptz;
alter table reply_examples add column if not exists priority integer not null default 0;
