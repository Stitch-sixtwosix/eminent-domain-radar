-- Prepaid postcard credits. A firm buys credits before it mails anything, so the money to pay the mail service is
-- always in hand. One credit is one postcard. Credits are not refunded and do not expire.
-- Every change is a row: a purchase adds, a postcard subtracts, a card the mail service refused gives one back.
create table if not exists public.credit_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users on delete cascade,
  delta integer not null,
  reason text not null check (reason in ('purchase', 'postcard', 'returned', 'granted')),
  ref text,
  stripe_session text unique,
  paid_cents integer not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists credit_ledger_user on public.credit_ledger (user_id, created_at desc);

alter table public.credit_ledger enable row level security;
drop policy if exists "own credits or admin" on public.credit_ledger;
create policy "own credits or admin" on public.credit_ledger for select to authenticated
  using (user_id = auth.uid() or (auth.jwt() -> 'app_metadata' ->> 'role') = 'admin');
grant select on public.credit_ledger to authenticated;
grant all on public.credit_ledger to service_role;
grant usage, select on all sequences in schema public to service_role;

create or replace function public.credit_balance(p_user uuid) returns integer
language sql stable security definer set search_path = '' as $$
  select coalesce(sum(delta), 0)::integer from public.credit_ledger where user_id = p_user
$$;

-- Takes one credit, or returns -1 if there is none. The lock makes two cards sent at the same instant queue up, so a
-- firm with one credit left cannot mail two.
create or replace function public.spend_credit(p_user uuid, p_ref text) returns integer
language plpgsql security definer set search_path = '' as $$
declare bal integer;
begin
  perform pg_advisory_xact_lock(hashtext('credit:' || p_user::text));
  select coalesce(sum(delta), 0) into bal from public.credit_ledger where user_id = p_user;
  if bal < 1 then return -1; end if;
  insert into public.credit_ledger (user_id, delta, reason, ref) values (p_user, -1, 'postcard', left(p_ref, 300));
  return bal - 1;
end $$;

revoke all on function public.credit_balance(uuid) from public, anon, authenticated;
revoke all on function public.spend_credit(uuid, text) from public, anon, authenticated;
grant execute on function public.credit_balance(uuid) to service_role;
grant execute on function public.spend_credit(uuid, text) to service_role;
