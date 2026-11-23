// Records a page view, a click worth knowing about, or how long a page was read. Called by track.js on every public page.
// Anyone can call it, so it trusts nothing: only requests from our own site are kept, bots are dropped, every field is
// cut to length, and it always answers the same way. The visitor's IP address is never stored.
import { clip, sha1Hex, visitRow } from "../_shared/core.js";
import { admin, SITE } from "../_shared/svc.ts";

const OWN = SITE.replace(/^https?:\/\//, "").replace(/^www\./, "");
const HEADERS = (origin: string) => ({
  "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "content-type", "Vary": "Origin", "Cache-Control": "no-store",
});

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
    const country = clip(req.headers.get("cf-ipcountry") || req.headers.get("x-vercel-ip-country") || req.headers.get("x-country-code"), 2);
    await db.from("visits").upsert({ ...row, country: country && /^[A-Z]{2}$/.test(country) ? country : null }, { onConflict: "vid", ignoreDuplicates: true });
  } catch (e) {
    console.error("track", e);
  }
  return done();
});
