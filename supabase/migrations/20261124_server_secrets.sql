-- Secrets the server obtains for itself, such as the signing secret Stripe returns when the server registers its own
-- webhook. Row security is on and there is no policy, so no signed-in person and no browser can read this table;
-- only the server functions, using the service key, can.
create table if not exists public.server_secrets (
  name text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);
alter table public.server_secrets enable row level security;
revoke all on public.server_secrets from anon, authenticated;
grant all on public.server_secrets to service_role;
