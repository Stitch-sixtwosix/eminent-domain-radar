// What the admin console knows about money comes from here, read straight from Stripe: the subscriptions that
// exist, what has been collected, what failed. Nothing on the console is called "paying" unless Stripe says so.
// Administrator only. The default action only reads. "hold", "release" and "delete" act on one account:
//   hold     closes the app for the account until it is released. Stripe keeps billing; holding is not cancelling.
//   release  reopens it, if it has a live subscription or had access that was opened by hand.
//   delete   cancels any live Stripe subscription at once, then removes the account and everything stored for it.
import { admin, caller, Refused, serve, setAccess, SITE, stripe, webhookSecrets } from "../_shared/svc.ts";

const LIVE = ["active", "trialing", "past_due"];        // a subscription Stripe is still collecting on

// deno-lint-ignore no-explicit-any
type Any = any;

Deno.serve((req) => serve(req, async () => {
  const user = await caller(req);
  if ((user.app_metadata || {}).role !== "admin") throw new Refused("Administrators only.", 403);

  const body = await req.json().catch(() => ({}));
  if (["hold", "release", "delete"].includes(body.action)) {
    const { data, error } = await admin().auth.admin.getUserById(String(body.user_id || ""));
    if (error || !data.user) throw new Refused("No such account.", 404);
    const target = data.user, meta = target.app_metadata || {};
    if (target.id === user.id) throw new Refused("You cannot hold or delete the account you are signed in with.");
    if (meta.role === "admin") throw new Refused("Remove the administrator role from this account first.");
    if (body.action === "hold") {
      if (meta.held) return { ok: true };
      await setAccess(target.id, { held: true, held_at: new Date().toISOString(), held_access: meta.access || "none", access: "none" });
      return { ok: true };
    }
    // Is Stripe still collecting for this account? Checked in Stripe, not taken from our own copy.
    let live: Any = null;
    if (meta.stripe_subscription) {
      const s = await stripe("GET", "subscriptions/" + meta.stripe_subscription).catch(() => null);
      if (s && LIVE.includes(s.status)) live = s;
    }
    if (body.action === "release") {
      await setAccess(target.id, { held: false, held_at: null, held_access: null, access: live || meta.held_access === "active" ? "active" : "none" });
      return { ok: true };
    }
    if (body.confirm_email !== target.email) throw new Refused("The email typed to confirm does not match this account.");
    if (live) await stripe("DELETE", "subscriptions/" + live.id);
    const gone = await admin().auth.admin.deleteUser(target.id);        // usage, saved wording, lookups and cases go with it (on delete cascade)
    if (gone.error) throw new Refused("The account could not be deleted: " + gone.error.message, 500);
    return { ok: true, cancelled_subscription: !!live };
  }

  if (body.action === "stripe_setup") {
    // Does for Stripe what would otherwise be copied by hand: registers this site's webhook and keeps the signing secret
    // Stripe returns, and switches on the page where a subscriber changes their card or cancels. Safe to run again.
    const key = Deno.env.get("STRIPE_SECRET_KEY") || "";
    if (!key) throw new Refused("Save the Stripe key on the server first.", 503);
    const mode = key.includes("_live_") ? "live" : "test", name = "stripe_webhook_" + mode;
    const url = (Deno.env.get("SUPABASE_URL") || "").replace(/\/$/, "") + "/functions/v1/stripe-webhook";
    const events = ["checkout.session.completed", "customer.subscription.updated", "customer.subscription.deleted", "invoice.paid"];
    const db = admin(), done: string[] = [];
    const have = await db.from("server_secrets").select("name").eq("name", name).maybeSingle();
    const hooks = await stripe("GET", "webhook_endpoints", { limit: 100 });
    const ours = (hooks.data || []).filter((h: Any) => h.url === url);
    const good = ours.find((h: Any) => h.status === "enabled" && events.every((e) => (h.enabled_events || []).includes(e) || (h.enabled_events || []).includes("*")));
    if (good && (have.data || Deno.env.get("STRIPE_WEBHOOK_SECRET"))) done.push("Stripe notifications were already set up.");
    else {
      // Stripe shows a signing secret only when a webhook is created, so one whose secret we do not hold is replaced.
      for (const h of ours) await stripe("DELETE", "webhook_endpoints/" + h.id);
      const made = await stripe("POST", "webhook_endpoints", { url, enabled_events: events, description: "EminentTracker: opens and closes accounts" });
      if (!made.secret) throw new Refused("Stripe created the webhook but returned no signing secret.", 502);
      const saved = await db.from("server_secrets").upsert({ name, value: made.secret, updated_at: new Date().toISOString() });
      if (saved.error) { await stripe("DELETE", "webhook_endpoints/" + made.id); throw new Refused("The signing secret could not be stored: " + saved.error.message, 500); }
      done.push("Stripe notifications are set up (" + mode + " mode).");
    }
    try {
      const cfg = await stripe("GET", "billing_portal/configurations", { is_default: true, limit: 1 });
      if ((cfg.data || []).length) done.push("The change-card-or-cancel page was already on.");
      else {
        await stripe("POST", "billing_portal/configurations", {
          business_profile: { headline: "EminentTracker subscription", privacy_policy_url: SITE + "/privacy.html", terms_of_service_url: SITE + "/terms.html" },
          features: { payment_method_update: { enabled: true }, invoice_history: { enabled: true },
            customer_update: { enabled: true, allowed_updates: ["email", "address", "name"] },
            subscription_cancel: { enabled: true, mode: "at_period_end" } },
          default_return_url: SITE + "/account.html" });
        done.push("The change-card-or-cancel page is switched on.");
      }
    } catch (e) { done.push("The change-card-or-cancel page could not be switched on: " + (e instanceof Error ? e.message : String(e))); }
    return { ok: true, mode, done };
  }

  if (body.action === "health") {
    // What the system status screen cannot see from the browser: which keys are saved, and whether each service answers.
    const key = Deno.env.get("STRIPE_SECRET_KEY") || "", lob = Deno.env.get("LOB_API_KEY") || "";
    const out: Any = {
      stripe_key: key.startsWith("sk_live_") || key.startsWith("rk_live_") ? "live" : key ? "test" : "off",
      webhook_secret: (await webhookSecrets()).length > 0,
      lob_key: lob.startsWith("live_") ? "live" : lob ? "test" : "off",
      batchdata_key: !!Deno.env.get("BATCHDATA_API_KEY"),
    };
    if (key) {
      try {
        const b = await stripe("GET", "balance");
        out.stripe = { ok: true, live: !!b.livemode };
        const acct = await stripe("GET", "account").catch(() => null);
        if (acct) Object.assign(out.stripe, { charges: !!acct.charges_enabled, payouts: !!acct.payouts_enabled, verified: !!acct.details_submitted });
        const hooks = await stripe("GET", "webhook_endpoints", { limit: 50 }).catch(() => null);
        if (hooks) out.stripe.webhook_endpoints = (hooks.data || []).filter((h: Any) => String(h.url).includes("/functions/v1/stripe-webhook") && h.status === "enabled").length;
      } catch (e) { out.stripe = { ok: false, why: e instanceof Error ? e.message : String(e) }; }
    }
    if (lob) {
      try {
        const r = await fetch("https://api.lob.com/v1/postcards?limit=1", { headers: { Authorization: "Basic " + btoa(lob + ":") } });
        out.lob = { ok: r.ok, why: r.ok ? null : "Lob answered " + r.status };
      } catch (_e) { out.lob = { ok: false, why: "Lob could not be reached" }; }
    }
    const db = admin(), count = async (table: string) => { const r = await db.from(table).select("*", { count: "exact", head: true }); return r.error ? null : r.count; };
    const last = await db.from("visits").select("at").order("at", { ascending: false }).limit(1);
    out.tables = { usage: await count("usage"), support_cases: await count("support_cases"), visits: await count("visits"), firm_outreach: await count("firm_outreach"), skip_results: await count("skip_results") };
    out.last_visit = last.error || !last.data.length ? null : last.data[0].at;
    return out;
  }

  const [subs, invoices, balance, users] = await Promise.all([
    stripe("GET", "subscriptions", { status: "all", limit: 100, expand: ["data.customer"] }),
    stripe("GET", "invoices", { limit: 100, expand: ["data.customer"] }),
    stripe("GET", "balance"),
    admin().auth.admin.listUsers({ page: 1, perPage: 1000 }),
  ]);
  const accounts = (users.data && users.data.users) || [];
  const byId = Object.fromEntries(accounts.map((u: Any) => [u.id, u]));
  const email = (c: Any) => (c && typeof c === "object" ? c.email : null) || null;

  const subscriptions = (subs.data || []).map((s: Any) => {
    const item = (s.items && s.items.data && s.items.data[0]) || {}, price = item.price || {};
    const interval = (price.recurring || {}).interval || "month", amount = (price.unit_amount || 0) * (item.quantity || 1);
    const m = s.metadata || {};
    return {
      id: s.id, status: s.status, user_id: m.user_id || null, email: email(s.customer) || ((byId[m.user_id] || {}).email ?? null),
      plan: m.plan || null, state: m.state || null, amount, interval, monthly: Math.round(interval === "year" ? amount / 12 : amount),
      started: s.start_date || s.created, renews: s.current_period_end || item.current_period_end || null,
      cancels_at_period_end: !!s.cancel_at_period_end, canceled_at: s.canceled_at || null,
    };
  });
  const live = subscriptions.filter((s: Any) => LIVE.includes(s.status));
  const month = Date.now() / 1000 - 30 * 86400;
  const bills = (invoices.data || []).map((i: Any) => ({
    id: i.id, number: i.number, created: i.created, email: email(i.customer) || i.customer_email || null, status: i.status,
    total: i.total || 0, paid: i.amount_paid || 0, due: i.amount_remaining || 0, attempts: i.attempt_count || 0,
    kind: i.billing_reason === "manual" ? "Postcards and lookups" : i.billing_reason === "subscription_create" ? "First payment" : "Renewal",
    url: i.hosted_invoice_url || null,
  }));
  const recent = bills.filter((b: Any) => b.created >= month);
  const sum = (list: Any[], k: string) => list.reduce((a, b) => a + (b[k] || 0), 0);

  // Where the site and Stripe disagree. Each line is something to look at, not something this function fixes.
  const liveBy = Object.fromEntries(live.filter((s: Any) => s.user_id).map((s: Any) => [s.user_id, s]));
  const mismatches: { email: string | null; what: string }[] = [];
  for (const u of accounts) {
    const m = u.app_metadata || {};
    if (m.role === "admin") continue;
    if (m.access === "active" && m.stripe_subscription && !liveBy[u.id]) mismatches.push({ email: u.email ?? null, what: "The site has this account open on a subscription, but Stripe has no live subscription for it." });
    if (m.access !== "active" && liveBy[u.id]) mismatches.push({ email: u.email ?? null, what: "Stripe is collecting on a subscription, but the account is closed on the site." });
  }
  for (const s of live) if (!s.user_id || !byId[s.user_id]) mismatches.push({ email: s.email, what: "A live Stripe subscription that belongs to no account on the site." });

  return {
    live_mode: !!balance.livemode,
    paying_user_ids: Object.keys(liveBy),
    totals: {
      live: live.length, mrr: sum(live.filter((s: Any) => s.status !== "trialing"), "monthly"),
      past_due: live.filter((s: Any) => s.status === "past_due").length,
      cancelling: live.filter((s: Any) => s.cancels_at_period_end).length,
      canceled_30d: subscriptions.filter((s: Any) => s.status === "canceled" && (s.canceled_at || 0) >= month).length,
      collected_30d: sum(recent.filter((b: Any) => b.status === "paid"), "paid"),
      open_due: sum(bills.filter((b: Any) => b.status === "open"), "due"),
      failed_30d: recent.filter((b: Any) => b.status === "open" && b.attempts > 0).length + recent.filter((b: Any) => b.status === "uncollectible").length,
      available: sum((balance.available || []).filter((b: Any) => b.currency === "usd"), "amount"),
      pending: sum((balance.pending || []).filter((b: Any) => b.currency === "usd"), "amount"),
    },
    subscriptions, invoices: bills.slice(0, 50), mismatches,
    more: !!(subs.has_more || invoices.has_more),
  };
}));
