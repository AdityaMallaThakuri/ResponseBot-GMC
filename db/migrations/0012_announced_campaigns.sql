-- 0012: which watched campaigns the bot has already announced in Slack, so a restart or a redeploy
-- never reposts them. kind 'watching' = our webhook is on the campaign; 'would_watch' = it would be
-- attached, but AUTO_ATTACH_WEBHOOKS is off. Safe to re-run.

create table if not exists announced_campaigns (
    id             uuid         primary key default gen_random_uuid(),
    campaign_id    bigint       not null,
    kind           text         not null,
    campaign_name  text         not null default '',
    created_at     timestamptz  not null default now(),
    constraint announced_campaigns_kind_check check (kind in ('watching', 'would_watch')),
    constraint announced_campaigns_campaign_kind_key unique (campaign_id, kind)
);

alter table announced_campaigns enable row level security;

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table public.%I from %I', 'announced_campaigns', r);
        end if;
    end loop;
end;
$$;
