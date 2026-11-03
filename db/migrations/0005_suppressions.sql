-- 0005: suppressions, recorded by the caller after a Smartlead unsubscribe/suppress action.
-- Moved from app/db/schema/suppressions.sql. The scope rules below are unchanged; the primary
-- key is now a uuid like every other table, and thread_id links a suppression to its thread.

create table if not exists suppressions (
    id              uuid        primary key default gen_random_uuid(),
    email           text        not null,
    scope           text        not null check (scope in ('campaign', 'client', 'global')),
    campaign_id     bigint,
    client          text,
    reason          text        not null,
    source_reply_id text,
    thread_id       uuid        references threads (id) on delete set null,
    confirmed_by    text,
    created_at      timestamptz not null default now(),
    constraint campaign_scope_needs_campaign check (scope <> 'campaign' or campaign_id is not null),
    constraint client_scope_needs_client check (scope <> 'client' or client is not null),
    -- a global suppression only ever follows a human confirmation in Slack
    constraint global_scope_needs_human check (scope <> 'global' or confirmed_by is not null)
);
create index if not exists suppressions_email_idx on suppressions (email);
create index if not exists suppressions_thread_id_idx on suppressions (thread_id);
create index if not exists suppressions_scope_idx on suppressions (scope);

alter table suppressions enable row level security;
