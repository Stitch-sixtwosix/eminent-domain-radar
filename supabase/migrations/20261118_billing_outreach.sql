-- Usage ledger, saved postcard wording and paid owner lookups. Run once in the Supabase SQL editor.
-- Only the server functions write to these tables (they use the service key, which skips row security).
-- From the browser a firm can read nothing here directly; the admin account can read the ledger.

create table if not exists public.firm_outreach (
  user_id uuid primary key references auth.users on delete cascade,
  firm jsonb not null default '{}',
  template jsonb not null default '{}',
  updated_at timestamptz not null default now()
);

create table if not exists public.usage (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users on delete cascade,
  kind text not null check (kind in ('postcard', 'skip_trace')),
  ref text not null,
  owner text,
  mode text not null default 'live',
  cost_cents integer not null default 0,
  price_cents integer not null default 0,
  external_id text,
  stripe_customer text,
  stripe_item text,
  invoiced boolean not null default false,
  detail jsonb,
  created_at timestamptz not null default now()
);
create index if not exists usage_user_time on public.usage (user_id, created_at desc);
create index if not exists usage_user_ref on public.usage (user_id, kind, ref);
create index if not exists usage_customer_open on public.usage (stripe_customer) where not invoiced;

create table if not exists public.skip_cache (
  parcel_key text primary key,
  result jsonb not null,
  fetched_at timestamptz not null default now()
);

create table if not exists public.skip_results (
  user_id uuid not null references auth.users on delete cascade,
  parcel_key text not null,
  result jsonb not null,
  created_at timestamptz not null default now(),
  primary key (user_id, parcel_key)
);

alter table public.firm_outreach enable row level security;
alter table public.usage enable row level security;
alter table public.skip_cache enable row level security;
alter table public.skip_results enable row level security;

drop policy if exists "admin reads usage" on public.usage;
create policy "admin reads usage" on public.usage for select to authenticated
  using ((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin');
