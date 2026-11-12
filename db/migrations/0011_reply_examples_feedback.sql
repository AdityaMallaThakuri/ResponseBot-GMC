-- 0011: the example library the bot drafts from, and Good draft / Bad draft feedback.
-- reply_examples holds one row per answered reply: the prospect's first reply (cleaned), the
-- reply Gamic actually sent, the campaign, our label and the date.
--   source 'history'   loaded from the past replies dataset (source_thread_id = Smartlead thread id)
--   source 'ray_sent'  learned from a reply Ray sent after we posted a draft (draft_id is that draft,
--                      draft_similarity 0 to 100 says how close our draft was to what he sent)
-- drafts.feedback records a click on "Good draft" or "Bad draft". Nothing else happens on a click.
-- Safe to re-run.

create table if not exists reply_examples (
    id                uuid         primary key default gen_random_uuid(),
    source            text         not null,
    source_thread_id  text         not null,
    thread_id         uuid         references threads (id) on delete set null,
    draft_id          uuid         references drafts (id) on delete set null,
    campaign_name     text         not null default '',
    label             text         not null,
    prospect_reply    text         not null,
    gamic_reply       text         not null,
    replied_at        timestamptz,
    draft_similarity  numeric(5,2),
    created_at        timestamptz  not null default now(),
    constraint reply_examples_source_check check (source in ('history', 'ray_sent')),
    constraint reply_examples_similarity_check check (draft_similarity between 0 and 100),
    constraint reply_examples_source_key unique (source, source_thread_id),
    constraint reply_examples_draft_key unique (draft_id)
);
create index if not exists reply_examples_label_idx on reply_examples (label, replied_at desc);
create index if not exists reply_examples_replied_at_idx on reply_examples (replied_at desc);

alter table reply_examples enable row level security;

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table public.%I from %I', 'reply_examples', r);
        end if;
    end loop;
end;
$$;

alter table drafts add column if not exists feedback text
    constraint drafts_feedback_check check (feedback in ('good', 'bad'));
alter table drafts add column if not exists feedback_by text;
alter table drafts add column if not exists feedback_at timestamptz;
