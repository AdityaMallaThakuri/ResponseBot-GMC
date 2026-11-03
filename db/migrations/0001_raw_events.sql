-- 0001: shared helpers and raw_events.
-- Plain Postgres 15+, works on Supabase. Safe to re-run. Never store API keys or secrets here.

create or replace function set_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- Every incoming payload exactly as received, for replay and debugging.
create table if not exists raw_events (
    id             uuid        primary key default gen_random_uuid(),
    source         text        not null,
    payload        jsonb       not null,
    payload_hash   text        not null,
    received_at    timestamptz not null default now(),
    processed      boolean     not null default false,
    processed_at   timestamptz,
    process_error  text,
    created_at     timestamptz not null default now(),
    constraint raw_events_source_check
        check (source in ('smartlead_webhook', 'smartlead_poll', 'slack', 'manual')),
    constraint raw_events_payload_hash_check check (payload_hash ~ '^[0-9a-f]{64}$'),
    -- a re-delivered identical payload is the same event
    constraint raw_events_source_hash_key unique (source, payload_hash)
);
create index if not exists raw_events_unprocessed_idx
    on raw_events (received_at) where not processed;
create index if not exists raw_events_received_at_idx on raw_events (received_at);

alter table raw_events enable row level security;
