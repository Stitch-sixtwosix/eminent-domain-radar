-- This project does not hand new tables to the server's role automatically, so the functions could read and write nothing.
-- The server role gets full use of the tables it owns the logic for; signed-in people get only what row security then allows.
grant usage on schema public to service_role, authenticated;
grant all on public.firm_outreach, public.usage, public.skip_cache, public.skip_results, public.support_cases, public.visits to service_role;
grant usage, select on all sequences in schema public to service_role, authenticated;
grant select on public.usage to authenticated;            -- row security lets only the administrator read it
grant select, insert on public.support_cases to authenticated;
grant update (status, admin_note, closed_at) on public.support_cases to authenticated;
grant select on public.visits to authenticated;
