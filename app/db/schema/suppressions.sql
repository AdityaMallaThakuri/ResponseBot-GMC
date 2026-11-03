-- Suppressions recorded by the caller after a Smartlead unsubscribe/suppress action.
create table if not exists suppressions (
    id              bigint generated always as identity primary key,
    email           text        not null,
    scope           text        not null check (scope in ('campaign', 'client', 'global')),
    campaign_id     bigint,
    client          text,
    reason          text        not null,
    source_reply_id text,
    confirmed_by    text,
    created_at      timestamptz not null default now(),
    constraint campaign_scope_needs_campaign check (scope <> 'campaign' or campaign_id is not null),
    constraint client_scope_needs_client check (scope <> 'client' or client is not null),
    -- a global suppression only ever follows a human confirmation in Slack
    constraint global_scope_needs_human check (scope <> 'global' or confirmed_by is not null)
);
create index if not exists suppressions_email_idx on suppressions (email);
