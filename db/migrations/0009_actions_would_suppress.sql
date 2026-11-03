-- 0009: actions may record a decision the bot would have taken but did not.
-- While SEND_ENABLED is off, a do_not_contact reply is recorded as 'would_suppress' and nothing is
-- sent to Smartlead. Safe to re-run.

alter table actions drop constraint if exists actions_type_check;
alter table actions add constraint actions_type_check
    check (type in ('suppress', 'unsubscribe', 'pause', 'ignore', 'send', 'undo', 'would_suppress'));
