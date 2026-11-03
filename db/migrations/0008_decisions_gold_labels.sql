-- 0008: extracted facts, vertical, template version, human decisions and the gold label set.
-- Templates stay in app/config/playbook.yaml (git is the version history): there is no
-- templates table and no events table. Does not edit 0001 to 0007.

-- Structured facts pulled from a reply, e.g. an out-of-office return date or a referred contact.
alter table classifications
    add column if not exists extracted_facts jsonb
        constraint classifications_extracted_facts_object
        check (jsonb_typeof(extracted_facts) = 'object');

alter table threads add column if not exists vertical text;
create index if not exists threads_vertical_idx on threads (vertical);

-- Which version of playbook.yaml produced the draft. Required whenever a draft has a template.
-- If drafts already holds rows, backfill template_version before applying this migration.
alter table drafts
    add column if not exists template_version text
        constraint drafts_template_version_needs_template
        check (template_id is null or template_version is not null);

-- One human decision per draft (a repeated Slack click cannot record a second one).
create table if not exists decisions (
    id                uuid         primary key default gen_random_uuid(),
    draft_id          uuid         not null references drafts (id) on delete cascade,
    -- Slack user id, e.g. U012ABCDEF
    decided_by        text         not null,
    decision          text         not null,
    skip_reason       text,
    final_text        text,
    edit_distance_pct numeric(5,2),
    decided_at        timestamptz  not null default now(),
    sent_message_id   uuid         references messages (id) on delete set null,
    created_at        timestamptz  not null default now(),
    constraint decisions_draft_id_key unique (draft_id),
    constraint decisions_decision_check check (decision in ('approved', 'edited', 'skipped')),
    constraint decisions_skip_reason_check
        check (skip_reason in ('wrong_label', 'bad_draft', 'not_worth_replying', 'other')),
    constraint decisions_decided_by_check check (decided_by ~ '^[UW][A-Z0-9]{2,}$'),
    constraint decisions_edit_distance_check check (edit_distance_pct between 0 and 100),
    constraint decisions_skip_reason_only_when_skipped
        check (skip_reason is null or decision = 'skipped'),
    constraint decisions_final_text_unless_skipped
        check (decision = 'skipped' or final_text is not null)
);
create index if not exists decisions_decision_idx on decisions (decision);
create index if not exists decisions_decided_at_idx on decisions (decided_at desc);
create index if not exists decisions_sent_message_idx on decisions (sent_message_id);

alter table decisions enable row level security;

-- Human-labelled threads: the gold set the eval replay scores the bot against.
-- A thread delete cascades here too, so erasing a lead's data leaves nothing behind.
create table if not exists gold_labels (
    id                  uuid        primary key default gen_random_uuid(),
    thread_id           uuid        not null references threads (id) on delete cascade,
    human_message_type  text        not null,
    human_verdict       text        not null,
    human_template      text,
    labeler             text        not null,
    notes               text,
    created_at          timestamptz not null default now(),
    constraint gold_labels_thread_labeler_key unique (thread_id, labeler),
    -- the same label set as classifications.label
    constraint gold_labels_human_message_type_check check (human_message_type in (
        'meeting_request', 'info_request', 'free_list_request', 'price_question',
        'price_and_info', 'lead_magnet_request', 'trial_request', 'booked_confirmation',
        'objection_info', 'objection_price', 'not_interested', 'wrong_person',
        'out_of_office', 'auto_reply_or_bounce', 'do_not_contact', 'off_script'
    )),
    constraint gold_labels_human_verdict_check check (human_verdict in ('pass', 'flag', 'hard_dq')),
    constraint gold_labels_human_template_check
        check (human_template in ('T1', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7', 'T8')),
    constraint gold_labels_labeler_check check (length(trim(labeler)) > 0)
);
create index if not exists gold_labels_thread_id_idx on gold_labels (thread_id);
create index if not exists gold_labels_message_type_idx on gold_labels (human_message_type);

alter table gold_labels enable row level security;

-- Same lockdown as 0007: only the service key. Guarded so it runs on plain Postgres.
do $$
declare
    t text;
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            foreach t in array array['decisions', 'gold_labels'] loop
                execute format('revoke all on table public.%I from %I', t, r);
            end loop;
        end if;
    end loop;
end;
$$;
