-- 0014: the vertical of each example, so M&A examples and SOP templates are never used for a
-- campaign that is not M&A (see app/config/verticals.yaml and app/examples/verticals.py).
--   ma, lenders, marketing, relocation, solar, facilities, other, unknown
-- Existing rows get 'unknown' (treated as NOT M&A) until scripts/backfill_verticals.py --apply
-- sets them: the backfill needs the Smartlead campaign tree, which SQL cannot see.
-- threads.vertical already exists (0002) and is filled when a thread is ingested.
-- Apply after 0013. Safe to re-run.

alter table reply_examples add column if not exists vertical text not null default 'unknown';
alter table reply_examples drop constraint if exists reply_examples_vertical_check;
alter table reply_examples add constraint reply_examples_vertical_check
    check (vertical in ('ma', 'lenders', 'marketing', 'relocation', 'solar', 'facilities',
                        'other', 'unknown'));
create index if not exists reply_examples_vertical_idx
    on reply_examples (vertical, label, replied_at desc);
