-- Support cases, and the fuller account list the admin page reads. Run after 20261118_billing_outreach.sql.

-- A signed-in person files a case from their account page. They can read their own; only the admin reads all and closes them.
create table if not exists public.support_cases (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  email text not null default (auth.jwt() ->> 'email'),
  topic text not null default 'other' check (topic in ('billing', 'data', 'postcards', 'lookups', 'account', 'other')),
  subject text not null check (char_length(subject) between 3 and 160),
  body text not null check (char_length(body) between 10 and 4000),
  status text not null default 'open' check (status in ('open', 'closed')),
  admin_note text,
  created_at timestamptz not null default now(),
  closed_at timestamptz
);
create index if not exists support_cases_open on public.support_cases (status, created_at desc);
create index if not exists support_cases_user on public.support_cases (user_id, created_at desc);

alter table public.support_cases enable row level security;

drop policy if exists "file own case" on public.support_cases;
create policy "file own case" on public.support_cases for insert to authenticated
  with check (user_id = auth.uid() and email = (auth.jwt() ->> 'email') and status = 'open' and admin_note is null and closed_at is null);

drop policy if exists "read own case or admin" on public.support_cases;
create policy "read own case or admin" on public.support_cases for select to authenticated
  using (user_id = auth.uid() or (auth.jwt() -> 'app_metadata' ->> 'role') = 'admin');

drop policy if exists "admin answers cases" on public.support_cases;
create policy "admin answers cases" on public.support_cases for update to authenticated
  using ((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin')
  with check ((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin');

grant select, insert on public.support_cases to authenticated;
grant update (status, admin_note, closed_at) on public.support_cases to authenticated;

-- Every account with what the admin page needs to count sign-ups and subscriptions. Returns nothing unless the caller is the admin.
-- Card and subscription are reported as yes/no; the Stripe identifiers themselves stay on the server.
create or replace function public.admin_accounts() returns setof jsonb
language sql stable security definer set search_path = '' as $$
  select jsonb_build_object(
    'id', u.id, 'email', u.email, 'created_at', u.created_at, 'confirmed', u.email_confirmed_at is not null,
    'invited', u.invited_at is not null, 'last_sign_in_at', u.last_sign_in_at,
    'provider', u.raw_app_meta_data ->> 'provider',
    'role', u.raw_app_meta_data ->> 'role', 'access', u.raw_app_meta_data ->> 'access',
    'plan', u.raw_app_meta_data ->> 'plan', 'state', u.raw_app_meta_data ->> 'state',
    'has_card', coalesce(u.raw_app_meta_data ->> 'stripe_customer', '') <> '',
    'has_subscription', coalesce(u.raw_app_meta_data ->> 'stripe_subscription', '') <> '',
    'paid_through', u.raw_app_meta_data -> 'paid_through',
    'held', coalesce((u.raw_app_meta_data ->> 'held')::boolean, false), 'held_at', u.raw_app_meta_data ->> 'held_at',
    'full_name', coalesce(u.raw_user_meta_data ->> 'full_name', u.raw_user_meta_data ->> 'name'),
    'firm', u.raw_user_meta_data ->> 'firm', 'location', u.raw_user_meta_data ->> 'location', 'phone', u.raw_user_meta_data ->> 'phone',
    'chosen_plan', u.raw_user_meta_data ->> 'plan', 'chosen_state', u.raw_user_meta_data ->> 'plan_state',
    'bill', u.raw_user_meta_data ->> 'bill', 'side', u.raw_user_meta_data ->> 'side',
    'agreed_terms_at', u.raw_user_meta_data ->> 'agreed_terms_at')
  from auth.users u
  where (auth.jwt() -> 'app_metadata' ->> 'role') = 'admin'
  order by u.created_at desc
$$;
revoke all on function public.admin_accounts() from public, anon;
grant execute on function public.admin_accounts() to authenticated;
