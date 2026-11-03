-- 0010: what a draft was made with, and the full validator result.
-- drafts.model: the model that filled the template's slots or chose the template (null if none).
-- drafts.validator_result: {"ok", "blocking", "approve_blockers", "warnings"} as last computed.
-- (validator_ok and validator_reasons stay: blocking reasons, kept for existing queries.)
-- Safe to re-run.

alter table drafts add column if not exists model text;
alter table drafts add column if not exists validator_result jsonb
    constraint drafts_validator_result_object check (jsonb_typeof(validator_result) = 'object');
