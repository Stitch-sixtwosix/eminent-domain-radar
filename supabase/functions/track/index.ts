// Records a page view, a click worth knowing about, or how long a page was read. Called by track.js on every public page.
// Anyone can call it, so it trusts nothing: only requests from our own site are kept, bots are dropped, every field is
// cut to length, and it always answers the same way. The visitor's IP address is never stored.
import { clip, isDatacenter, sha1Hex, visitRow } from "../_shared/core.js";
import { admin, SITE } from "../_shared/svc.ts";

const OWN = SITE.replace(/^https?:\/\//, "").replace(/^www\./, "");
const HEADERS = (origin: string) => ({
  "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type", "Vary": "Origin", "Cache-Control": "no-store",
});

// City, state and network for a visit. Looked up once per visit from the visitor's address by GeoJS, a free lookup
// service; later pages of the same visit reuse the answer. The address goes to that service for the lookup and is
// not kept by us. A lookup that fails or is slow simply leaves the place blank.
// deno-lint-ignore no-explicit-any
async function place(db: any, session: string | null, ip: string) {
  const none = { country: null, region: null, city: null, org: null };
  try {
    if (session) {
      const seen = await db.from("visits").select("country, region, city, org").eq("session", session).not("country", "is", null).limit(1);
      if (!seen.error && seen.data.length) return seen.data[0];
    }
    if (!/^[0-9a-f:.]{3,45}$/i.test(ip) || /^(10\.|127\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$|f[cd])/i.test(ip)) return none;
    const stop = new AbortController(), timer = setTimeout(() => stop.abort(), 2500);
    const r = await fetch("https://get.geojs.io/v1/ip/geo/" + encodeURIComponent(ip) + ".json", { signal: stop.signal });
    clearTimeout(timer);
    if (!r.ok) return none;
    const g = await r.json();
    return { country: /^[A-Z]{2}$/.test(String(g.country_code || "")) ? g.country_code : null, region: clip(g.region, 80), city: clip(g.city, 80),
             org: clip(String(g.organization_name || "").replace(/^AS\d+\s*/, ""), 120) };
  } catch (_e) { return none; }
}

Deno.serve(async (req) => {
  const origin = req.headers.get("origin") || "";
  const ours = origin.replace(/^https?:\/\//, "").replace(/^www\./, "") === OWN;
  const done = () => new Response(null, { status: 204, headers: HEADERS(ours ? origin : SITE) });
  if (req.method !== "POST" || !ours) return done();
  try {
    const text = await req.text();
    if (text.length > 4000) return done();
    const body = JSON.parse(text), ua = req.headers.get("user-agent") || "";
    // The page sends a random id it keeps in the browser. Without one (storage blocked), a day-long id is made from the
    // address and browser so the same person is not counted on every page; the address itself is thrown away.
    const ip = (req.headers.get("x-forwarded-for") || "").split(",")[0].trim();
    const visitor = /^[a-z0-9]{12,40}$/i.test(String(body.v || "")) ? String(body.v)
      : "d" + (await sha1Hex(new Date().toISOString().slice(0, 10) + "|" + ip + "|" + ua + "|" + (Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || ""))).slice(0, 20);
    const db = admin();
    if (body.k === "ping") {              // the reader left the page: how long they stayed and how far they scrolled
      const seconds = Math.max(0, Math.min(3600, Math.round(Number(body.secs) || 0))), scroll = Math.max(0, Math.min(100, Math.round(Number(body.scroll) || 0)));
      if (/^[a-z0-9]{8,40}$/i.test(String(body.vid || "")))
        await db.from("visits").update({ seconds, scroll }).eq("vid", String(body.vid)).gte("at", new Date(Date.now() - 6 * 3600e3).toISOString());
      return done();
    }
    const row = visitRow(body, ua, OWN, visitor);
    if (!row) return done();
    const where = await place(db, row.session, ip);
    if (isDatacenter(where.org)) return done();          // a crawler or link checker on a server network, not a visitor
    await db.from("visits").upsert({ ...row, ...where }, { onConflict: "vid", ignoreDuplicates: true });
  } catch (e) {
    console.error("track", e);
  }
  return done();
});
