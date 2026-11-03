-- 0002: threads (one per lead per campaign) and messages.

create table if not exists threads (
    id                    uuid        primary key default gen_random_uuid(),
    email_lead_id         bigint      not null,
    email_lead_map_id     bigint,
    email_campaign_id     bigint      not null,
    campaign_name         text,
    lead_email            text        not null,
    lead_first_name       text,
    lead_last_name        text,
    company               text,
    -- Smartlead's own category at last sight; a hint only, the bot classifies for itself
    smartlead_category_id bigint,
    -- Smartlead's lead_status as received (external vocabulary, so no CHECK)
    smartlead_lead_status text,
    -- our pipeline state for the thread
    status                text        not null default 'open',
    last_reply_at         timestamptz,
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    constraint threads_status_check
        check (status in ('open', 'drafted', 'awaiting_human', 'auto_handled', 'suppressed', 'closed')),
    constraint threads_lead_campaign_key unique (email_lead_id, email_campaign_id)
);
create index if not exists threads_status_idx on threads (status);
create index if not exists threads_campaign_idx on threads (email_campaign_id);
create index if not exists threads_lead_email_idx on threads (lead_email);
create index if not exists threads_last_reply_at_idx on threads (last_reply_at desc);

create or replace trigger threads_set_updated_at
    before update on threads
    for each row execute function set_updated_at();

alter table threads enable row level security;

-- stats_id is NOT unique per message: a SENT and the REPLY to it can share one.
-- Idempotency therefore keys on (thread_id, direction, sent_at, message_id), where message_id
-- is the email Message-ID header. It is NOT NULL because NULLs never collide in a unique
-- constraint, which would let a replayed webhook create duplicates.
create table if not exists messages (
    id            uuid        primary key default gen_random_uuid(),
    thread_id     uuid        not null references threads (id) on delete cascade,
    direction     text        not null,
    sent_at       timestamptz not null,
    stats_id      text        not null,
    message_id    text        not null,
    from_address  text        not null,
    to_address    text        not null,
    body_clean    text        not null,
    body_html     text,
    created_at    timestamptz not null default now(),
    constraint messages_direction_check check (direction in ('SENT', 'REPLY')),
    constraint messages_thread_direction_sent_at_message_id_key
        unique (thread_id, direction, sent_at, message_id)
);
create index if not exists messages_thread_id_idx on messages (thread_id, sent_at);
create index if not exists messages_stats_id_idx on messages (stats_id);
create index if not exists messages_direction_idx on messages (thread_id, direction);

alter table messages enable row level security;
