// Postcards (Lob) and paid owner lookups (BatchData) for subscribers, with a usage ledger.
// Postcards are prepaid: a firm buys credits first and each card mailed takes one, so the money to pay the mail service is
// in hand before anything is sent. Credits are not refunded and do not expire.
// Owner lookups are added to the firm's next Stripe invoice; once the unbilled amount reaches BILL_AT_CENTS it is
// invoiced and charged straight away. No lookup is run for an account with an unpaid invoice.
//
// A test Lob key (test_...) renders proofs and never mails or bills. Mailing starts when LOB_API_KEY is a live key.
import {
  CREDIT_PACKS, creditPack, DEFAULT_TEMPLATE, FIRM_FIELDS, firmReady, ownerKind, parcelKey, postcardCheck, RATES, RESTRICTED_STATES, sha1Hex, skipRequest, skipResult,
} from "../_shared/core.js";
import { admin, caller, mustBeActive, Refused, serve, SITE, stripe } from "../_shared/svc.ts";

const cents = (name: string, fallback: number) => Number(Deno.env.get(name) || fallback);
const PRICE = { postcard: cents("RATE_POSTCARD_PRICE", RATES.postcard.price), skip_trace: cents("RATE_SKIP_TRACE_PRICE", RATES.skip_trace.price) };
const COST = { postcard: cents("RATE_POSTCARD_COST", RATES.postcard.cost), skip_trace: cents("RATE_SKIP_TRACE_COST", RATES.skip_trace.cost) };
const BILL_AT = cents("BILL_AT_CENTS", 5000);
const DAILY_CARDS = cents("DAILY_POSTCARD_LIMIT", 1000);
const TEMPLATE_KEYS = [...Object.keys(DEFAULT_TEMPLATE), "email_subject", "letter"];     // the letter is printed by the firm itself
const lobKey = () => Deno.env.get("LOB_API_KEY") || "";
const lobMode = () => (lobKey().startsWith("live_") ? "live" : lobKey() ? "test" : "off");

type User = Awaited<ReturnType<typeof caller>>;
const isAdmin = (u: User) => (u.app_metadata || {}).role === "admin";     // our own account: nothing to bill

async function credits(userId: string): Promise<number> {
  const { data, error } = await admin().rpc("credit_balance", { p_user: userId });
  return error ? 0 : Number(data || 0);
}

/** Sends the firm to Stripe to buy a pack of postcard credits. The credits are added when Stripe confirms the payment. */
async function buyCredits(user: User, body: any) {
  const pack = creditPack(body.pack);
  if (!pack) throw new Refused("Choose one of the credit packs.");
  const customer = (user.app_metadata || {}).stripe_customer as string | undefined;
  const s = await stripe("POST", "checkout/sessions", {
    mode: "payment", client_reference_id: user.id, ...(customer ? { customer } : { customer_email: user.email }),
    line_items: [{ quantity: 1, price_data: { currency: "usd", unit_amount: pack.cents,
      product_data: { name: pack.credits + " postcard credits", description: "One credit mails one 4x6 postcard, first class, in your firm's name. Prepaid; not refundable; credits do not expire." } } }],
    metadata: { user_id: user.id, kind: "postcard_credits", credits: pack.credits },
    payment_intent_data: { description: pack.credits + " EminentTracker postcard credits", metadata: { user_id: user.id, kind: "postcard_credits", credits: pack.credits } },
    custom_text: { submit: { message: "Postcard credits are prepaid and are not refundable. They do not expire." } },
    success_url: SITE + "/app/?credits=bought", cancel_url: SITE + "/app/?credits=cancelled",
  });
  return { url: s.url };
}

async function saved(userId: string) {
  const { data } = await admin().from("firm_outreach").select("firm, template").eq("user_id", userId).maybeSingle();
  return { firm: (data && data.firm) || {}, template: (data && data.template) || {} };
}

/** Refuse paid work for an account that cannot be billed, before anything is mailed or bought. */
async function billable(user: User): Promise<string | null> {
  if (isAdmin(user)) return null;
  const customer = (user.app_metadata || {}).stripe_customer as string | undefined;
  if (!customer) throw new Refused("Paid outreach needs a card on file. Add one from your account page first.", 402);
  const open = await stripe("GET", "invoices", { customer, status: "open", limit: 1 });
  if ((open.data || []).length) throw new Refused("Your last invoice has not been paid. Update your card from your account page, then try again.", 402);
  return customer;
}

/** Write the ledger line and put the charge on the firm's next invoice. Past BILL_AT the pending amount is charged now. */
async function bill(user: User, customer: string | null, row: Record<string, unknown>, description: string) {
  const db = admin(), price = Number(row.price_cents || 0);
  let item: string | null = null;
  if (customer && price > 0) {
    try {
      item = (await stripe("POST", "invoiceitems", { customer, amount: price, currency: "usd", description, metadata: { user_id: user.id, kind: row.kind, ref: row.ref } },
        "ii-" + (await sha1Hex(user.id + "|" + row.kind + "|" + row.ref + "|" + (row.external_id || ""))))).id;
    } catch (e) { console.error("invoice item failed", e); }       // the ledger still records it, so it can be billed by hand
  }
  await db.from("usage").insert({ ...row, user_id: user.id, stripe_customer: customer, stripe_item: item, invoiced: !customer || price === 0 });
  if (!customer || !item) return;
  const { data } = await db.from("usage").select("price_cents").eq("user_id", user.id).eq("invoiced", false);
  const pending = (data || []).reduce((a: number, r: { price_cents: number }) => a + r.price_cents, 0);
  // With a subscription the charges ride on the next invoice. Without one there is no next invoice, so each order is charged as it is made.
  if (pending < BILL_AT && (user.app_metadata || {}).stripe_subscription) return;
  try {
    const inv = await stripe("POST", "invoices", { customer, collection_method: "charge_automatically", pending_invoice_items_behavior: "include", auto_advance: true,
      description: "EminentTracker postcards and owner lookups" });
    await stripe("POST", `invoices/${inv.id}/pay`);
    await db.from("usage").update({ invoiced: true }).eq("user_id", user.id).eq("invoiced", false);
  } catch (e) { console.error("usage invoice failed", e); }        // left open: billable() blocks further orders until it is paid
}

async function status(user: User) {
  const s = await saved(user.id), mode = lobMode();
  const month = new Date(); month.setUTCDate(1); month.setUTCHours(0, 0, 0, 0);
  const { data } = await admin().from("usage").select("kind, price_cents, mode, invoiced").eq("user_id", user.id).gte("created_at", month.toISOString());
  const rows = (data || []).filter((r: any) => r.mode === "live");
  const sum = (kind: string) => rows.filter((r: any) => r.kind === kind);
  const { data: bought } = await admin().from("skip_results").select("parcel_key, result, created_at").eq("user_id", user.id).order("created_at", { ascending: false }).limit(5000);
  return {
    lookups: Object.fromEntries((bought || []).map((b: any) => [b.parcel_key, { ...b.result, fetched_at: b.created_at }])),
    firm: s.firm, template: s.template, firm_ready: firmReady(s.firm),
    postcard: { enabled: mode !== "off", mode, price: PRICE.postcard / 100, why: mode === "off" ? "Postcard mailing is being switched on." : null },
    credits: { balance: isAdmin(user) ? null : await credits(user.id),
      packs: Object.entries(CREDIT_PACKS).map(([key, p]: [string, any]) => ({ key, credits: p.credits, price: p.cents / 100, each: Math.round(p.cents / p.credits) / 100 })) },
    skiptrace: { enabled: !!Deno.env.get("BATCHDATA_API_KEY"), price: PRICE.skip_trace / 100, restricted: RESTRICTED_STATES,
      why: Deno.env.get("BATCHDATA_API_KEY") ? null : "Owner lookups are being switched on." },
    usage: { postcards: sum("postcard").length, lookups: sum("skip_trace").length, billed: rows.reduce((a: number, r: any) => a + r.price_cents, 0) / 100,
      pending: rows.filter((r: any) => !r.invoiced).reduce((a: number, r: any) => a + r.price_cents, 0) / 100 },
  };
}

async function saveTemplate(user: User, body: any) {
  const firm = Object.fromEntries(FIRM_FIELDS.map((k: string) => [k, String((body.firm || {})[k] || "").trim().slice(0, 200)]));
  const template = Object.fromEntries(TEMPLATE_KEYS.map((k) => [k, String((body.template || {})[k] || "").trim().slice(0, k === "letter" ? 6000 : 1000)]).filter(([, v]) => v));
  const { error } = await admin().from("firm_outreach").upsert({ user_id: user.id, firm, template, updated_at: new Date().toISOString() });
  if (error) throw new Refused("Not saved: " + error.message, 500);
  return status(user);
}

const leadRef = (lead: any) => [lead.state, lead.parcel_county, lead.apn || lead.address, lead.owner].map((x) => String(x || "").trim().toUpperCase()).join("|").slice(0, 300);

async function postcard(user: User, body: any, send: boolean) {
  const lead = body.lead || {}, s = await saved(user.id), mode = lobMode();
  const check = postcardCheck(s.firm, s.template, lead), ref = leadRef(lead);
  const { data: before } = await admin().from("usage").select("created_at").eq("user_id", user.id).eq("kind", "postcard").eq("mode", "live").eq("ref", ref)
    .order("created_at", { ascending: false }).limit(1);
  const mailed = before && before[0] ? before[0].created_at : null;
  const view = { ...check, mode, price: PRICE.postcard / 100, already_mailed: mailed, credits: isAdmin(user) ? null : await credits(user.id) };
  if (!send) return view;

  if (check.blocker) throw new Refused(check.blocker);
  if (mode === "off") throw new Refused("Postcard mailing is being switched on.", 503);
  if (mailed && !body.again) throw new Refused("Your firm already mailed this owner a postcard on " + String(mailed).slice(0, 10) + ".");
  const live = mode === "live", prepaid = live && !isAdmin(user), db = admin();
  if (live) {
    const day = new Date(Date.now() - 864e5).toISOString();
    const { count } = await admin().from("usage").select("id", { count: "exact", head: true }).eq("user_id", user.id).eq("kind", "postcard").gte("created_at", day);
    if ((count || 0) >= DAILY_CARDS) throw new Refused("Your firm has reached today's postcard limit. Email us to raise it.", 429);
  }
  const to = check.to!, f = s.firm;
  const fields: Record<string, string> = {
    description: "EminentTracker " + ref.slice(0, 200),
    "to[name]": check.to_name, "to[address_line1]": to.line1, "to[address_line2]": to.line2, "to[address_city]": to.city, "to[address_state]": to.state, "to[address_zip]": to.zip,
    "from[name]": String(f.name).slice(0, 40), "from[address_line1]": f.address_line1, "from[address_city]": f.city, "from[address_state]": f.state, "from[address_zip]": String(f.zip),
    front: check.front, back: check.back, size: "4x6", use_type: "marketing", "metadata[user_id]": user.id,
  };
  const form = new URLSearchParams(Object.entries(fields).filter(([, v]) => v));
  // The same card to the same owner from the same firm is one order, however many times the button is pressed.
  const idem = "et-" + mode + "-" + (await sha1Hex(user.id + ref + check.front + check.back + (body.again ? String(mailed) : "")));
  // One credit is taken before the card is ordered and given back if the mail service refuses it, so a card is never
  // mailed that was not paid for and a credit is never kept for a card that was not mailed.
  if (prepaid) {
    const left = await db.rpc("spend_credit", { p_user: user.id, p_ref: ref });
    if (left.error) throw new Refused("Your credits could not be checked. Nothing was mailed. Try again in a minute.", 500);
    if (Number(left.data) < 0) throw new Refused("You have no postcard credits left. Buy credits on the Outreach tab, then send this card.", 402);
  }
  const giveBack = (why: string) => prepaid ? db.from("credit_ledger").insert({ user_id: user.id, delta: 1, reason: "returned", ref: (why + ": " + ref).slice(0, 300) }) : Promise.resolve();
  let r: Response, out: any;
  try {
    r = await fetch("https://api.lob.com/v1/postcards", { method: "POST", body: form,
      headers: { Authorization: "Basic " + btoa(lobKey() + ":"), "Idempotency-Key": idem } });
    out = await r.json().catch(() => ({}));
  } catch (_e) {
    await giveBack("mail service unreachable");
    throw new Refused("The mail service could not be reached. Nothing was mailed and your credit was returned.", 502);
  }
  if (!r.ok) {
    await giveBack("mail service declined");
    throw new Refused("The mail service declined the postcard: " + ((out.error && out.error.message) || r.status) + ". Nothing was mailed and your credit was returned.", 502);
  }
  // Two presses at the same instant reach the mail service as one order. The second must not cost a second credit.
  const twin = await db.from("usage").select("id").eq("user_id", user.id).eq("external_id", out.id).limit(1);
  if (twin.data && twin.data.length) { await giveBack("same card ordered twice"); return { mode, proof_url: out.url, expected_delivery: out.expected_delivery_date, charged: 0, credits: prepaid ? await credits(user.id) : null }; }
  await db.from("usage").insert({ user_id: user.id, kind: "postcard", ref, owner: String(lead.owner || "").slice(0, 120), mode, external_id: out.id,
    cost_cents: live ? COST.postcard : 0, price_cents: prepaid ? PRICE.postcard : 0, invoiced: true,       // paid for already, with a credit
    detail: { lead_id: lead.id, to, proof_url: out.url, expected_delivery: out.expected_delivery_date, paid_with: prepaid ? "credit" : null } });
  return { mode, proof_url: out.url, expected_delivery: out.expected_delivery_date, charged: 0, credit_used: prepaid ? 1 : 0, credits: prepaid ? await credits(user.id) : null };
}

async function skipTrace(user: User, body: any, buy: boolean) {
  const parcel = body.parcel || {}, key = parcelKey(parcel), db = admin();
  const state = String(parcel.state || "").toUpperCase();
  const { data: own } = await db.from("skip_results").select("result, created_at").eq("user_id", user.id).eq("parcel_key", key).maybeSingle();
  if (own) return { ...own.result, fetched_at: own.created_at, bought: true };
  if (!buy) return { bought: false, price: PRICE.skip_trace / 100, restricted: RESTRICTED_STATES.includes(state) };

  if (RESTRICTED_STATES.includes(state)) throw new Refused("Owner lookups are not offered in this state: its law limits using owner records for solicitation.");
  if (ownerKind(parcel.owner) === "government") throw new Refused("This parcel is owned by a public body.");
  const apiKey = Deno.env.get("BATCHDATA_API_KEY");
  if (!apiKey) throw new Refused("Owner lookups are being switched on.", 503);
  const request = skipRequest(parcel);
  if (!request || !key.replace(/\|/g, "")) throw new Refused("There is no address or parcel ID to search with.");
  const customer = await billable(user);

  let result, fresh = false;
  const { data: cached } = await db.from("skip_cache").select("result").eq("parcel_key", key).maybeSingle();
  if (cached) result = cached.result;
  else {
    const r = await fetch("https://api.batchdata.com/api/v1/property/skip-trace", { method: "POST", body: JSON.stringify(request),
      headers: { Authorization: "Bearer " + apiKey, "Content-Type": "application/json", Accept: "application/json" } });
    if (!r.ok) throw new Refused("The lookup service did not answer (" + r.status + "). Nothing was charged.", 502);
    result = skipResult(await r.json());
    fresh = true;
    await db.from("skip_cache").upsert({ parcel_key: key, result, fetched_at: new Date().toISOString() });
  }
  if (!result.matched) return { ...result, bought: false, charged: 0 };       // no match, no charge
  await db.from("skip_results").upsert({ user_id: user.id, parcel_key: key, result });
  await bill(user, customer, { kind: "skip_trace", ref: key.slice(0, 300), owner: String(result.name || "").slice(0, 120), mode: "live", external_id: null,
    cost_cents: fresh ? COST.skip_trace : 0, price_cents: customer ? PRICE.skip_trace : 0, detail: { lead_id: body.lead_id } }, "Owner lookup, parcel " + (parcel.apn || parcel.address || ""));
  return { ...result, bought: true, charged: customer ? PRICE.skip_trace / 100 : 0, fetched_at: new Date().toISOString() };
}

async function usage(user: User) {
  const { data } = await admin().from("usage").select("created_at, kind, owner, mode, price_cents, invoiced").eq("user_id", user.id).order("created_at", { ascending: false }).limit(500);
  return { rows: (data || []).map((r: any) => ({ created_at: r.created_at, kind: r.kind, owner: r.owner, mode: r.mode, price: r.price_cents / 100, invoiced: r.invoiced })) };
}

Deno.serve((req) => serve(req, async () => {
  const user = await caller(req);
  mustBeActive(user);
  const body = await req.json().catch(() => ({}));
  switch (body.action) {
    case "status": return status(user);
    case "save_template": return saveTemplate(user, body);
    case "postcard_preview": return postcard(user, body, false);
    case "postcard_send": return postcard(user, body, true);
    case "skiptrace_lookup": return skipTrace(user, body, false);
    case "skiptrace_buy": return skipTrace(user, body, true);
    case "usage": return usage(user);
    case "credits_checkout": return buyCredits(user, body);
    default: throw new Refused("Unknown action.");
  }
}));
