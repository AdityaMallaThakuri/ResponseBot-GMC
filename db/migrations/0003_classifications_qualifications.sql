-- 0003: classifier and qualifier output.

create table if not exists classifications (
    id                          uuid         primary key default gen_random_uuid(),
    thread_id                   uuid         not null references threads (id) on delete cascade,
    -- the REPLY message that was classified
    reply_message_id            uuid         not null references messages (id) on delete cascade,
    label                       text         not null,
    confidence                  numeric(4,3) not null,
    reason                      text         not null,
    model                       text         not null,
    prompt_version              text         not null,
    -- the Smartlead category id seen at classification time; a hint only
    smartlead_category_id       bigint,
    agrees_with_smartlead       boolean,
    -- name of the pre-filter rule that decided this, if any
    rule_hit                    text,
    created_at                  timestamptz  not null default now(),
    -- the initial label set from CLAUDE.md; Phase B changes it through a new migration
    constraint classifications_label_check check (label in (
        'meeting_request', 'info_request', 'free_list_request', 'price_question',
        'price_and_info', 'lead_magnet_request', 'trial_request', 'booked_confirmation',
        'objection_info', 'objection_price', 'not_interested', 'wrong_person',
        'out_of_office', 'auto_reply_or_bounce', 'do_not_contact', 'off_script'
    )),
    constraint classifications_confidence_check check (confidence between 0 and 1)
);
create index if not exists classifications_thread_id_idx on classifications (thread_id);
create index if not exists classifications_reply_message_idx on classifications (reply_message_id);
create index if not exists classifications_label_idx on classifications (label);
create index if not exists classifications_disagree_idx
    on classifications (thread_id) where agrees_with_smartlead is false;

alter table classifications enable row level security;

create table if not exists qualifications (
    id          uuid        primary key default gen_random_uuid(),
    thread_id   uuid        not null references threads (id) on delete cascade,
    verdict     text        not null,
    reasons     jsonb       not null default '[]'::jsonb,
    icp_version text        not null,
    created_at  timestamptz not null default now(),
    constraint qualifications_verdict_check check (verdict in ('pass', 'flag', 'hard_dq')),
    constraint qualifications_reasons_array check (jsonb_typeof(reasons) = 'array')
);
create index if not exists qualifications_thread_id_idx on qualifications (thread_id);
create index if not exists qualifications_verdict_idx on qualifications (verdict);

alter table qualifications enable row level security;
