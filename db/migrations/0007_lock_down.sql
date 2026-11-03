-- 0007: only the service key may touch these tables.
-- RLS is already enabled on every table with no policies, which denies anon and authenticated.
-- This also removes their table privileges as a second layer. Guarded so it runs on plain
-- Postgres, where those Supabase roles do not exist. The service_role bypasses RLS.

do $$
declare
    t text;
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            foreach t in array array[
                'raw_events', 'threads', 'messages', 'classifications', 'qualifications',
                'drafts', 'slack_cards', 'actions', 'suppressions', 'retry_queue', 'eval_results'
            ] loop
                execute format('revoke all on table public.%I from %I', t, r);
            end loop;
            execute format('revoke execute on function public.set_updated_at() from %I', r);
        end if;
    end loop;
end;
$$;
