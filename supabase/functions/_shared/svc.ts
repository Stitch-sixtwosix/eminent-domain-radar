// What the functions share that touches the network: who is calling, the admin client, Stripe.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.57.4";
import { form } from "./core.js";

export const SITE = (Deno.env.get("SITE_URL") || "https://eminenttracker.com").replace(/\/$/, "");
const ALLOWED = [SITE, "http://localhost:8767", "http://127.0.0.1:8767"];

export function cors(req: Request): Record<string, string> {
  const origin = req.headers.get("origin") || "";
  return {
    "Access-Control-Allow-Origin": ALLOWED.includes(origin) ? origin : SITE,
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}
export const reply = (req: Request, body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors(req), "Content-Type": "application/json" } });

export class Refused extends Error {
  status: number;
  constructor(message: string, status = 400) { super(message); this.status = status; }
}

export const admin = () =>
  createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, { auth: { persistSession: false, autoRefreshToken: false } });

/** The signed-in person, checked with Supabase itself. Nothing the browser says about who it is is trusted. */
export async function caller(req: Request) {
  const token = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!token) throw new Refused("Sign in first.", 401);
  const { data, error } = await admin().auth.getUser(token);
  if (error || !data.user) throw new Refused("Your sign-in has expired. Sign in again.", 401);
  return data.user;
}
export function mustBeActive(user: { app_metadata?: Record<string, unknown> }) {
  if ((user.app_metadata || {}).access !== "active") throw new Refused("This needs an active subscription.", 402);
}

export async function stripe(method: "GET" | "POST", path: string, params: Record<string, unknown> = {}, idempotencyKey?: string) {
  const key = Deno.env.get("STRIPE_SECRET_KEY");
  if (!key) throw new Refused("Online payment is not switched on yet.", 503);
  const body = form(params);
  const r = await fetch("https://api.stripe.com/v1/" + path + (method === "GET" && body ? "?" + body : ""), {
    method,
    headers: { Authorization: "Bearer " + key, "Content-Type": "application/x-www-form-urlencoded", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) },
    body: method === "POST" ? body : undefined,
  });
  const out = await r.json();
  if (!r.ok) throw new Refused((out.error && out.error.message) || "Stripe returned " + r.status, r.status === 402 ? 402 : 502);
  return out;
}

/** Merge into the fields only we can write. Supabase replaces app_metadata key by key, so the rest is carried over. */
export async function setAccess(userId: string, change: Record<string, unknown>) {
  const a = admin();
  const { data, error } = await a.auth.admin.getUserById(userId);
  if (error || !data.user) throw new Refused("No such account: " + userId, 404);
  const { error: e2 } = await a.auth.admin.updateUserById(userId, { app_metadata: { ...(data.user.app_metadata || {}), ...change } });
  if (e2) throw new Refused("The account could not be updated: " + e2.message, 500);
}

export async function serve(req: Request, run: () => Promise<unknown>) {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors(req) });
  try {
    return reply(req, await run());
  } catch (e) {
    if (e instanceof Refused) return reply(req, { error: e.message }, e.status);
    console.error(e);
    return reply(req, { error: "Something went wrong on our side. Try again in a minute." }, 500);
  }
}
