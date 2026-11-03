-- 0006: retry_queue and eval_results.

create table if not exists retry_queue (
    id              uuid        primary key default gen_random_uuid(),
    kind            text        not null,
    payload         jsonb       not null,
    attempts        integer     not null default 0,
    next_attempt_at timestamptz not null default now(),
    last_error      text,
    -- 'pending' until done, or 'dead' once the retries are exhausted
    status          text        not null default 'pending',
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint retry_queue_kind_check check (kind in (
        'process_event', 'classify', 'slack_post', 'smartlead_action', 'send_reply'
    )),
    constraint retry_queue_status_check check (status in ('pending', 'done', 'dead')),
    constraint retry_queue_attempts_check check (attempts >= 0),
    constraint retry_queue_payload_object check (jsonb_typeof(payload) = 'object')
);
create index if not exists retry_queue_due_idx
    on retry_queue (next_attempt_at) where status = 'pending';
create index if not exists retry_queue_kind_idx on retry_queue (kind);

create or replace trigger retry_queue_set_updated_at
    before update on retry_queue
    for each row execute function set_updated_at();

alter table retry_queue enable row level security;

create table if not exists eval_results (
    id          uuid        primary key default gen_random_uuid(),
    run_id      text        not null,
    scorecard   jsonb       not null,
    git_sha     text        not null,
    created_at  timestamptz not null default now(),
    constraint eval_results_run_id_key unique (run_id),
    constraint eval_results_git_sha_check check (git_sha ~ '^[0-9a-f]{7,40}$'),
    constraint eval_results_scorecard_object check (jsonb_typeof(scorecard) = 'object')
);
create index if not exists eval_results_created_at_idx on eval_results (created_at desc);

alter table eval_results enable row level security;
