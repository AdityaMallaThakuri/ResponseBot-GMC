-- 0004: drafts, Slack cards and actions.

create table if not exists drafts (
    id                uuid        primary key default gen_random_uuid(),
    thread_id         uuid        not null references threads (id) on delete cascade,
    -- NO ACTION (not RESTRICT): a thread delete cascades to both tables consistently,
    -- while deleting a classification on its own cannot orphan a draft.
    classification_id uuid        not null references classifications (id),
    template_id       text        not null,
    slots             jsonb       not null default '{}'::jsonb,
    text              text        not null,
    validator_ok      boolean     not null,
    validator_reasons jsonb       not null default '[]'::jsonb,
    human_edited      boolean     not null default false,
    final_text        text,
    status            text        not null default 'proposed',
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    constraint drafts_status_check
        check (status in ('proposed', 'approved', 'edited', 'skipped', 'sent', 'failed')),
    constraint drafts_slots_object check (jsonb_typeof(slots) = 'object'),
    constraint drafts_validator_reasons_array check (jsonb_typeof(validator_reasons) = 'array'),
    -- a draft that failed the validator can never be approved, edited or sent
    constraint drafts_validator_failure_blocks_send
        check (validator_ok or status in ('skipped', 'failed')),
    constraint drafts_edit_needs_final_text check (not human_edited or final_text is not null)
);
create index if not exists drafts_thread_id_idx on drafts (thread_id);
create index if not exists drafts_classification_id_idx on drafts (classification_id);
create index if not exists drafts_status_idx on drafts (status);

create or replace trigger drafts_set_updated_at
    before update on drafts
    for each row execute function set_updated_at();

alter table drafts enable row level security;

create table if not exists slack_cards (
    id          uuid        primary key default gen_random_uuid(),
    thread_id   uuid        not null references threads (id) on delete cascade,
    draft_id    uuid        references drafts (id) on delete cascade,
    lane        text        not null,
    channel     text        not null,
    slack_ts    text,
    status      text        not null default 'pending',
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint slack_cards_lane_check check (lane in ('draft', 'auto_action', 'human_only')),
    constraint slack_cards_status_check
        check (status in ('pending', 'posted', 'approved', 'edited', 'skipped', 'resolved', 'failed')),
    constraint slack_cards_draft_lane_needs_draft check (lane <> 'draft' or draft_id is not null)
);
create index if not exists slack_cards_thread_id_idx on slack_cards (thread_id);
create index if not exists slack_cards_draft_id_idx on slack_cards (draft_id);
create index if not exists slack_cards_lane_status_idx on slack_cards (lane, status);
create unique index if not exists slack_cards_channel_ts_key
    on slack_cards (channel, slack_ts) where slack_ts is not null;

create or replace trigger slack_cards_set_updated_at
    before update on slack_cards
    for each row execute function set_updated_at();

alter table slack_cards enable row level security;

create table if not exists actions (
    id            uuid        primary key default gen_random_uuid(),
    thread_id     uuid        not null references threads (id) on delete cascade,
    type          text        not null,
    -- 'bot' or 'human:<slack user id>'
    triggered_by  text        not null,
    reversible    boolean     not null,
    reversed_at   timestamptz,
    details       jsonb       not null default '{}'::jsonb,
    created_at    timestamptz not null default now(),
    constraint actions_type_check
        check (type in ('suppress', 'unsubscribe', 'pause', 'ignore', 'send', 'undo')),
    constraint actions_triggered_by_check
        check (triggered_by = 'bot' or triggered_by ~ '^human:.+'),
    constraint actions_details_object check (jsonb_typeof(details) = 'object'),
    constraint actions_reversed_needs_reversible check (reversed_at is null or reversible)
);
create index if not exists actions_thread_id_idx on actions (thread_id);
create index if not exists actions_type_idx on actions (type);
create index if not exists actions_reversible_open_idx
    on actions (thread_id) where reversible and reversed_at is null;

alter table actions enable row level security;
