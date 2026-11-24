-- Where a visitor is: city, state and the network they are on, looked up from their address when a visit starts.
-- The address itself is still not stored.
alter table public.visits add column if not exists region text, add column if not exists city text, add column if not exists org text;

create or replace function public.admin_traffic(days integer default 30) returns jsonb
language sql stable security definer set search_path = '' as $$
  with ok as (select (auth.jwt() -> 'app_metadata' ->> 'role') = 'admin' as yes),
  v as (select x.* from public.visits x, ok where ok.yes and x.kind = 'view' and x.at >= now() - make_interval(days => greatest(1, least(days, 730)))),
  e as (select x.* from public.visits x, ok where ok.yes and x.kind = 'event' and x.at >= now() - make_interval(days => greatest(1, least(days, 730)))),
  s as (select session, visitor, count(*) as pages, min(at) as started, sum(coalesce(seconds, 0)) as secs,
               (array_agg(path order by at))[1] as entry, (array_agg(path order by at desc))[1] as leave,
               (array_agg(coalesce(ref_host, '') order by at))[1] as source
        from v group by session, visitor)
  select jsonb_build_object(
    'days', days,
    'totals', (select jsonb_build_object(
        'views', count(*), 'visitors', count(distinct visitor), 'new_visitors', count(distinct visitor) filter (where is_new),
        'avg_seconds', coalesce(round(avg(seconds) filter (where seconds > 0)), 0),
        'avg_scroll', coalesce(round(avg(scroll) filter (where scroll > 0)), 0),
        'today_views', count(*) filter (where at >= date_trunc('day', now() at time zone 'America/New_York') at time zone 'America/New_York'),
        'today_visitors', count(distinct visitor) filter (where at >= date_trunc('day', now() at time zone 'America/New_York') at time zone 'America/New_York'),
        'live', count(distinct visitor) filter (where at >= now() - interval '5 minutes')) from v),
    'all_time', (select jsonb_build_object('views', count(*), 'visitors', count(distinct visitor), 'since', min(at))
                 from public.visits x, ok where ok.yes and x.kind = 'view'),
    'sessions', (select jsonb_build_object('count', count(*), 'bounces', count(*) filter (where pages = 1),
        'pages_per', coalesce(round(avg(pages), 1), 0), 'avg_seconds', coalesce(round(avg(secs)), 0),
        'returning', count(distinct visitor) filter (where visitor in (select visitor from s group by visitor having count(*) > 1))) from s),
    'by_day', (select coalesce(jsonb_agg(to_jsonb(x) order by x.day), '[]'::jsonb) from (
        select (at at time zone 'America/New_York')::date as day, count(*) as views, count(distinct visitor) as visitors, count(distinct session) as sessions
        from v group by 1) x),
    'by_hour', (select coalesce(jsonb_agg(to_jsonb(x) order by x.hour), '[]'::jsonb) from (
        select extract(hour from at at time zone 'America/New_York')::int as hour, count(*) as views from v group by 1) x),
    'pages', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select path as name, count(*) as views, count(distinct visitor) as visitors, coalesce(round(avg(seconds) filter (where seconds > 0)), 0) as seconds,
               coalesce(round(avg(scroll) filter (where scroll > 0)), 0) as scroll
        from v group by 1 order by 2 desc limit 60) x),
    'entries', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select entry as name, count(*) as sessions, count(*) filter (where pages = 1) as bounces from s group by 1 order by 2 desc limit 25) x),
    'exits', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select leave as name, count(*) as sessions from s group by 1 order by 2 desc limit 25) x),
    'sources', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select case when source = '' then 'Direct, or the source was not sent' else source end as name, count(*) as sessions, count(distinct visitor) as visitors,
               coalesce(round(avg(pages), 1), 0) as pages_per
        from s group by 1 order by 2 desc limit 40) x),
    'campaigns', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select utm_source as source, coalesce(utm_medium, '') as medium, coalesce(utm_campaign, '') as campaign, count(*) as views, count(distinct visitor) as visitors
        from v where utm_source is not null group by 1, 2, 3 order by 4 desc limit 40) x),
    'devices', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select coalesce(device, 'Unknown') as name, count(distinct visitor) as visitors, count(*) as views from v group by 1 order by 2 desc) x),
    'browsers', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select coalesce(browser, 'Unknown') as name, count(distinct visitor) as visitors, count(*) as views from v group by 1 order by 2 desc limit 15) x),
    'systems', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select coalesce(os, 'Unknown') as name, count(distinct visitor) as visitors, count(*) as views from v group by 1 order by 2 desc limit 15) x),
    'timezones', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select coalesce(tz, 'Unknown') as name, count(distinct visitor) as visitors, count(*) as views from v group by 1 order by 2 desc limit 30) x),
    'states', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select region as name, coalesce(country, '') as country, count(distinct visitor) as visitors, count(*) as views
        from v where region is not null group by 1, 2 order by 3 desc, 4 desc limit 60) x),
    'cities', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select city as name, coalesce(region, '') as region, coalesce(country, '') as country, count(distinct visitor) as visitors, count(*) as views
        from v where city is not null group by 1, 2, 3 order by 4 desc, 5 desc limit 100) x),
    'networks', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select org as name, count(distinct visitor) as visitors, count(*) as views from v where org is not null group by 1 order by 2 desc, 3 desc limit 60) x),
    'countries', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select country as name, count(distinct visitor) as visitors, count(*) as views from v where country is not null group by 1 order by 2 desc limit 30) x),
    'languages', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select coalesce(lang, 'Unknown') as name, count(distinct visitor) as visitors, count(*) as views from v group by 1 order by 2 desc limit 15) x),
    'screens', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select coalesce(screen, 'Unknown') as name, count(distinct visitor) as visitors, count(*) as views from v group by 1 order by 2 desc limit 15) x),
    'events', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (select event as name, count(*) as times, count(distinct visitor) as visitors from e group by 1 order by 2 desc limit 40) x),
    'recent', (select coalesce(jsonb_agg(to_jsonb(x)), '[]'::jsonb) from (
        select at, kind, path, event, coalesce(ref_host, '') as ref_host, coalesce(utm_source, '') as utm_source, device, browser, os, tz, country, region, city, org,
               seconds, scroll, is_new, right(visitor, 6) as visitor
        from public.visits y, ok where ok.yes and y.at >= now() - make_interval(days => greatest(1, least(days, 730))) order by at desc limit 250) x))
$$;
revoke all on function public.admin_traffic(integer) from public, anon;
grant execute on function public.admin_traffic(integer) to authenticated;
