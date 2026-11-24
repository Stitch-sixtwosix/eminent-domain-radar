// Pure logic shared by the functions: plans and prices, Stripe signatures, owner and address rules, postcard artwork.
// Nothing here touches the network, so all of it is covered by core_test.ts.

export const PLANS = {
  single: { name: "EminentTracker, single state", cents: 49900, year_cents: 479040 },
  all: { name: "EminentTracker, all 50 states", cents: 99900, year_cents: 959040 },
};
// What a subscriber pays per action, in cents, and what it costs us. Override with RATE_* secrets.
export const RATES = { postcard: { price: 145, cost: 91 }, skip_trace: { price: 75, cost: 20 } };

// States whose public-records laws bar using owner data for commercial solicitation. Owner lookups are not sold there.
export const RESTRICTED_STATES = ["ID", "MT", "SC", "WA"];

export const US_STATES = {
  alabama: "AL", alaska: "AK", arizona: "AZ", arkansas: "AR", california: "CA", colorado: "CO", connecticut: "CT", delaware: "DE",
  "district of columbia": "DC", florida: "FL", georgia: "GA", hawaii: "HI", idaho: "ID", illinois: "IL", indiana: "IN", iowa: "IA",
  kansas: "KS", kentucky: "KY", louisiana: "LA", maine: "ME", maryland: "MD", massachusetts: "MA", michigan: "MI", minnesota: "MN",
  mississippi: "MS", missouri: "MO", montana: "MT", nebraska: "NE", nevada: "NV", "new hampshire": "NH", "new jersey": "NJ",
  "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", ohio: "OH", oklahoma: "OK", oregon: "OR",
  pennsylvania: "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", tennessee: "TN", texas: "TX", utah: "UT",
  vermont: "VT", virginia: "VA", washington: "WA", "west virginia": "WV", wisconsin: "WI", wyoming: "WY",
};

/** The plan a sign-up asked for. A single-state plan needs a real state; anything else is refused. */
export function planChoice(plan, state) {
  const p = String(plan || "").toLowerCase();
  if (p === "all" || p.startsWith("all")) return { plan: "all", state: "" };
  const s = String(state || "").trim().toLowerCase().replace(/-/g, " ");
  if ((p === "single" || p.startsWith("single")) && US_STATES[s]) return { plan: "single", state: s };
  return null;
}

/** What a subscription's status means for access. Stripe keeps retrying a past-due card, so access stays on until it gives up. */
export function accessFor(status) {
  return ["active", "trialing", "past_due"].includes(status) ? "active" : "none";
}

/** Stripe's nested form encoding: {a: {b: 1}, c: [2]} -> a[b]=1&c[0]=2 */
export function form(params, prefix = "") {
  const out = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    const key = prefix ? `${prefix}[${k}]` : k;
    if (typeof v === "object") out.push(form(v, key));
    else out.push(encodeURIComponent(key) + "=" + encodeURIComponent(String(v)));
  }
  return out.filter(Boolean).join("&");
}

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
export async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text)));
}
export async function sha1Hex(text) {
  return hex(await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text)));
}
function sameText(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
/** True only for a body Stripe signed with our secret within the last five minutes. */
export async function stripeSigned(body, header, secret, now = Date.now() / 1000) {
  if (!header || !secret) return false;
  const parts = header.split(",").map((p) => p.trim().split("="));
  const t = (parts.find((p) => p[0] === "t") || [])[1];
  const sigs = parts.filter((p) => p[0] === "v1").map((p) => p[1]);
  if (!t || !sigs.length || Math.abs(now - Number(t)) > 300) return false;
  const want = await hmacHex(secret, `${t}.${body}`);
  return sigs.some((s) => sameText(s, want));
}

// ---------------------------------------------------------------- owners and addresses (mirrors edradar/outreach.py)
const GOVERNMENT =
  /\b(COMMONWEALTH|STATE OF|CITY OF|TOWN OF|VILLAGE OF|COUNTY OF|COUNTY|BOROUGH|TOWNSHIP|TWP|SCHOOL DIST|AUTHORITY|AUTH\b|UNITED STATES|U ?S ?A\b|DEPT\b|DEPARTMENT|TURNPIKE|REDEVELOPMENT|MUNICIPAL|HOUSING AUTH|PORT AUTH)/i;
const COMPANY =
  /\b(LLC|L L C|INC|CORP|CORPORATION|COMPANY|CO\b|LP\b|L P\b|LLP|LTD|PARTNERS|PARTNERSHIP|ASSOC|ASSN|ENTERPRISES|PROPERTIES|HOLDINGS|REALTY|BANK|CHURCH|TRUSTEES?|FOUNDATION|GROUP|INVESTMENTS?|RAILROAD|DEVELOPMENT|MANAGEMENT|ASSOCIATES|CLUB|FUND|VENTURES|CAPITAL)\b/i;
export function ownerKind(owner) {
  if (!owner) return "unknown";
  if (GOVERNMENT.test(owner)) return "government";
  return COMPANY.test(owner) ? "company" : "individual";
}

const ADDRESS_TAIL = /^(.*?)[,\s]+([A-Z]{2})[,\s]+(\d{5})(?:-?\d{4})?[,\s\d-]*$/;
const STREET_START = /^(\d|P\.? ?O\.? ?BOX|PO BOX|RR |HC |ONE |TWO )/i;
const SUFFIXES = new Set(["RD", "ROAD", "ST", "STREET", "AVE", "AVENUE", "DR", "DRIVE", "LN", "LANE", "BLVD", "HWY", "PIKE", "WAY", "CT", "COURT", "PL", "CIR", "TER", "PKWY", "N", "S", "E", "W"]);
/** Split a one-line mailing address. Strict on purpose: null rather than a guess, so a card is never mailed to a wrong address. */
export function parseAddress(text) {
  if (!text) return null;
  const m = ADDRESS_TAIL.exec(text.toUpperCase().split(/\s+/).filter(Boolean).join(" "));
  if (!m) return null;
  let head = m[1].split(",").map((p) => p.trim()).filter(Boolean);
  if (!head.length) return null;
  if (head.length === 1) {
    const words = head[0].split(" ");
    if (words.length < 3) return null;
    let cut = -1;
    words.forEach((w, i) => { if (SUFFIXES.has(w.replace(/^\.+|\.+$/g, "")) || /^\d+$/.test(w)) cut = i; });
    if (cut < 1 || cut >= words.length - 1) return null;
    head = [words.slice(0, cut + 1).join(" "), words.slice(cut + 1).join(" ")];
  }
  const city = head[head.length - 1], lines = head.slice(0, -1);
  const careOf = lines.filter((x) => /^(C\/O|ATTN|%)/.test(x)), streets = lines.filter((x) => !careOf.includes(x));
  const line1 = [...streets].reverse().find((s) => STREET_START.test(s));
  if (!line1 || /\d/.test(city)) return null;
  const extra = streets.filter((s) => s !== line1);
  return { line1: line1.slice(0, 64), line2: [...careOf, ...extra].join(", ").slice(0, 64), city: city.slice(0, 40), state: m[2], zip: m[3] };
}

// ---------------------------------------------------------------- the postcard
export const FIRM_FIELDS = ["name", "attorney", "phone", "email", "website", "address_line1", "city", "state", "zip"];
export const DEFAULT_TEMPLATE = {
  postcard_headline: "A public project is planned near {{property}}.",
  postcard_message:
    "{{project_name}} is in {{project_stage}}. Projects like this can require the government to acquire private land or easements. Owners are entitled to just compensation, and the first offer is not always the full amount.",
  postcard_back:
    "Know your rights before you sign. Public records show your parcel ({{parcel_id}}) lies along the planned route. {{firm_name}} represents property owners, not the agencies taking the land. A first conversation costs nothing: {{firm_phone}}.",
};
const KEEP_UPPER = new Set(["LLC", "LP", "LLP", "PC", "INC", "II", "III", "IV", "USA"]);
export const titleCase = (text) =>
  (text || "").split(/\s+/).filter(Boolean).map((w) =>
    /[a-z]/.test(w) || KEEP_UPPER.has(w.replace(/[.,]/g, "")) ? w : w.length > 1 && /[A-Z]/.test(w) ? w[0] + w.slice(1).toLowerCase() : w
  ).join(" ");
// Postcard credits are bought in packs before anything is mailed. One credit is one postcard.
export const CREDIT_PACKS = {
  // $1.45, $1.40 and $1.35 a card. The mail service costs about $0.91 and the card processor 2.9% plus 30 cents of
  // each purchase, which leaves roughly 49, 45 and 41 cents a card.
  small: { credits: 25, cents: 3625, label: "25 postcards" },
  medium: { credits: 100, cents: 14000, label: "100 postcards" },
  large: { credits: 500, cents: 67500, label: "500 postcards" },
};
export const creditPack = (key) => (Object.prototype.hasOwnProperty.call(CREDIT_PACKS, String(key)) ? CREDIT_PACKS[key] : null);

export const STAGE_PHRASE = { planning: "planning", environmental_review: "environmental review", design: "design",
  row_acquisition: "right-of-way acquisition", condemnation_filed: "the condemnation stage", construction: "construction" };

export function mergeValues(firm, lead) {
  const addr = lead.address && /^\s*\d/.test(lead.address) ? titleCase(lead.address) : "";
  return {
    owner_name: titleCase(lead.owner) || "the owner of record",
    property: addr ? `your property at ${addr}` : `your property (parcel ${lead.apn || ""})`,
    property_address: addr || `parcel ${lead.apn || ""}`,
    parcel_id: String(lead.apn || ""), county: titleCase(lead.parcel_county),
    project_name: (lead.project || "").replace(/\s*\(MPMS \d+\)/, "").replace(/\s*\([^)]*$/, "").trim() || "a planned public project",
    project_stage: STAGE_PHRASE[lead.stage] || "the agency's published plans",      // an unknown stage is never stated as one
    agency: lead.agency || "the sponsoring agency",
    firm_name: firm.name || "[Your firm name]", attorney_name: firm.attorney || firm.name || "[Attorney name]",
    firm_phone: firm.phone || "[phone]", firm_email: firm.email || "[email]", firm_website: firm.website || "",
  };
}
export const render = (text, values) =>
  (text || "").replace(/\{\{\s*(\w+)\s*\}\}/g, (all, k) => (k in values ? values[k] : all));
const esc = (v) => String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");

// Lob prints a 4x6 card from a 6.25 x 4.25 in page and trims 0.125 in from every side. Its print engine is an older one,
// so the artwork uses only absolute positioning and floats, and keeps text 0.3 in from the edge.
const CARD_CSS = `*{box-sizing:border-box;margin:0;padding:0}
html,body{width:6.25in;height:4.25in}
body{position:relative;overflow:hidden;font-family:Georgia,'Times New Roman',serif;color:#14202b}
.safe{position:absolute;top:0.3in;left:0.3in;width:5.65in;height:3.65in}`;
export function postcardHtml(firm, template, lead) {
  const tpl = { ...DEFAULT_TEMPLATE, ...Object.fromEntries(Object.entries(template || {}).filter(([k, v]) => k in DEFAULT_TEMPLATE && v)) };
  const v = mergeValues(firm, lead), headline = render(tpl.postcard_headline, v);
  const h1 = headline.length <= 62 ? 25 : headline.length <= 90 ? 21 : 17;
  const firmAddr = ["address_line1", "city", "state", "zip"].map((k) => firm[k]).filter(Boolean).join(", ");
  const front = `<html><head><style>${CARD_CSS}
body{background:#f4f5f7}
h1{font-size:${h1}pt;line-height:1.14;font-weight:700;width:5.3in}
p{font-family:Helvetica,Arial,sans-serif;font-size:11pt;line-height:1.4;margin-top:0.17in;width:5.1in}
.firm{position:absolute;left:0;bottom:0;width:5.65in;border-top:1.5pt solid #14202b;padding-top:0.09in;font-family:Helvetica,Arial,sans-serif;font-size:10.5pt;font-weight:bold}
.firm .ph{float:right;font-weight:normal}
</style></head><body><div class="safe">
<h1>${esc(headline)}</h1>
<p>${esc(render(tpl.postcard_message, v).slice(0, 420))}</p>
<div class="firm"><span class="ph">${esc(firm.phone || "")}</span>${esc(v.firm_name)}</div>
</div></body></html>`;
  // "ADVERTISEMENT." is always printed: lawyer advertising rules require it and a subscriber cannot switch it off.
  const back = `<html><head><style>${CARD_CSS}
body{background:#fff}
.msg{position:absolute;top:0;left:0;width:5.65in;height:1.3in;overflow:hidden;font-family:Helvetica,Arial,sans-serif;font-size:10pt;line-height:1.42}
.fine{position:absolute;left:0;bottom:0;width:2.15in;font-family:Helvetica,Arial,sans-serif;font-size:6.5pt;line-height:1.3;color:#3d4a57}
</style></head><body><div class="safe">
<div class="msg">${esc(render(tpl.postcard_back, v).slice(0, 430))}</div>
<div class="fine">ADVERTISEMENT. ${esc(v.firm_name)}, ${esc(firmAddr)}. This notice is based on public planning records. No taking of your property has been announced, and receiving this card does not mean one will occur.</div>
</div></body></html>`;
  return { front, back };
}

export const firmReady = (firm) => ["name", "address_line1", "city", "state", "zip"].every((k) => (firm[k] || "").trim());

/** Everything the confirmation step shows: who the card goes to, the artwork, and anything that stops it. */
export function postcardCheck(firm, template, lead) {
  const kind = ownerKind(lead.owner), to = parseAddress(lead.mailing_address);
  let blocker = null;
  if (kind === "government") blocker = "This parcel is owned by a public body.";
  else if (kind === "unknown") blocker = "No owner of record for this parcel.";
  else if (!to) blocker = "The mailing address could not be read reliably. Confirm it with the county first.";
  else if (!firmReady(firm)) blocker = "Add your firm's name and return address under Outreach, then Edit postcard wording.";
  return { owner_kind: kind, to_name: (lead.owner || "").slice(0, 40), to, blocker, ...postcardHtml(firm, template, lead) };
}

// ---------------------------------------------------------------- skip tracing (BatchData property skip trace)
/** One key per parcel, so the same lookup is never paid for twice. */
export const parcelKey = (p) =>
  [p.state, p.county, p.apn || p.address].map((x) => String(x || "").trim().toUpperCase().replace(/\s+/g, " ")).join("|");
export function skipRequest(p) {
  const state = String(p.state || "").toUpperCase();
  const req = {};
  const mail = parseAddress(p.mailing_address);
  if (mail) req.propertyAddress = { street: mail.line1, city: mail.city, state: mail.state, zip: mail.zip };   // where the owner gets mail
  else if (p.address && /^\s*\d/.test(p.address) && (p.city || p.zip)) req.propertyAddress = { street: p.address.trim(), city: p.city || "", state, zip: p.zip || "" };
  else if (p.apn && p.county && state) Object.assign(req, { apn: String(p.apn), county: titleCase(String(p.county)), state });
  else return null;
  return { requests: [req], options: { includeTCPABlacklistedPhones: false } };
}
export function skipResult(body) {
  const persons = ((body || {}).results || {}).persons || [];
  const person = persons.find((p) => ((p.meta || {}).matched ?? true) && (p.name || p.mailingAddress || (p.phoneNumbers || []).length || (p.emails || []).length));
  if (!person) return { matched: false, name: null, mailing_address: null, phones: [], emails: [], litigator: false };
  const n = person.name || {}, m = person.mailingAddress || {};
  const name = [n.first, n.last].filter(Boolean).map((x) => titleCase(String(x).toUpperCase())).join(" ") || null;
  const mailing = m.street ? [m.street, m.city, m.state, m.zip].filter(Boolean).join(", ").toUpperCase() : null;
  const phones = (person.phoneNumbers || []).filter((p) => p.number).map((p) => ({
    number: p.number, type: p.type, reachable: p.reachable, score: p.score, dnc: !!p.dnc, tcpa: !!p.tcpa,
  })).sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 5);
  const emails = (person.emails || []).map((e) => e.email).filter(Boolean).slice(0, 5);
  const matched = !!(name || mailing || phones.length || emails.length);
  return { matched, name, mailing_address: mailing, phones, emails, litigator: !!person.litigator };
}

// ---------- site visits ----------
export const isBot = (ua) => !ua || /bot|crawl|spider|slurp|preview|monitor|uptime|headless|lighthouse|pingdom|curl|wget|python|httpclient|scrapy|facebookexternalhit|whatsapp|go-http|java\/|axios|node-fetch|phantom|semrush|ahrefs|gptbot|claude|bytespider/i.test(ua);

// Networks that host servers, not people. A "visit" from one is a link checker or a crawler dressed as a browser:
// Facebook and Instagram fetch a page whenever its link is shared, and scanners run from rented cloud machines.
export const isDatacenter = (org) => /amazon|\baws\b|facebook|\bmeta\b|google|microsoft|azure|digitalocean|\bovh\b|hetzner|linode|akamai|cloudflare|oracle|alibaba|tencent|leaseweb|vultr|choopa|datacamp|\bm247\b|gtt communications|contabo|scaleway|hosting|datacenter|data center|colocation|\bcdn\b|server/i.test(String(org || ""));

/** Device, browser and system from a user-agent string. Coarse on purpose: enough to see who is visiting, no more. */
export function uaInfo(ua) {
  ua = String(ua || "");
  const device = /ipad|tablet|kindle|silk|playbook/i.test(ua) || (/android/i.test(ua) && !/mobile/i.test(ua)) ? "Tablet"
    : /mobi|iphone|ipod|android|windows phone/i.test(ua) ? "Phone" : "Computer";
  const browser = /edg(e|a|ios)?\//i.test(ua) ? "Edge" : /opr\/|opera/i.test(ua) ? "Opera" : /samsungbrowser/i.test(ua) ? "Samsung Internet"
    : /firefox|fxios/i.test(ua) ? "Firefox" : /crios|chrome|chromium/i.test(ua) ? "Chrome" : /safari/i.test(ua) ? "Safari"
    : /msie|trident/i.test(ua) ? "Internet Explorer" : "Other";
  const os = /windows nt/i.test(ua) ? "Windows" : /iphone|ipad|ipod/i.test(ua) ? "iOS" : /android/i.test(ua) ? "Android"
    : /mac os x|macintosh/i.test(ua) ? "macOS" : /cros/i.test(ua) ? "ChromeOS" : /linux/i.test(ua) ? "Linux" : "Other";
  return { device, browser, os };
}

/** The site a visitor came from, or "" when they came from this site or from nowhere we are told about. */
export function refHost(referrer, ownHost) {
  const m = /^https?:\/\/([^\/:?#]+)/i.exec(String(referrer || ""));
  const h = m ? m[1].replace(/^www\./, "").toLowerCase() : "";
  return !h || h === String(ownHost || "").replace(/^www\./, "").toLowerCase() ? "" : h;
}

export const clip = (v, n) => (v === undefined || v === null || v === "" ? null : String(v).replace(/[\u0000-\u001f]/g, " ").slice(0, n));

/** A visit as it is stored, from what the page sent. Null when it is not a visit we keep. */
export function visitRow(body, ua, ownHost, visitor) {
  if (!body || typeof body !== "object" || isBot(ua)) return null;
  const path = clip(body.path, 200);
  if (!path || !path.startsWith("/") || !/^[a-z0-9]{8,40}$/i.test(String(body.vid || ""))) return null;
  const info = uaInfo(ua), w = Number(body.sw) || 0, h = Number(body.sh) || 0;
  const row = {
    vid: String(body.vid), kind: body.k === "event" ? "event" : "view", visitor, is_new: body.fresh === true,
    session: clip(body.sid, 40), path, title: clip(body.title, 160), event: body.k === "event" ? clip(body.event, 80) : null,
    ref_host: refHost(body.ref, ownHost) || null, referrer: refHost(body.ref, ownHost) ? clip(body.ref, 300) : null,
    utm_source: clip(body.us, 80), utm_medium: clip(body.um, 80), utm_campaign: clip(body.uc, 120), utm_term: clip(body.ut, 80), utm_content: clip(body.ux, 80),
    device: info.device, browser: info.browser, os: info.os, lang: clip(body.lang, 20), tz: clip(body.tz, 60),
    screen: w && h ? `${w} x ${h}` : null,
  };
  return row.kind === "event" && !row.event ? null : row;
}
