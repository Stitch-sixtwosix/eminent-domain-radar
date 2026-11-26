// Run with macOS's own JavaScript engine (no install needed):
//   /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc -m supabase/functions/_shared/core_test.js
// or `deno run supabase/functions/_shared/core_test.js`. The signature check is skipped where the engine has no Web Crypto.
import * as C from "./core.js";

const say = typeof print === "function" ? print : console.log;
let failed = 0, ran = 0;
const eq = (name, got, want) => {
  ran++;
  if (JSON.stringify(got) !== JSON.stringify(want)) { failed++; say(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};

// plans
eq("single plan needs a real state", C.planChoice("Single state ($499 a month)", "New York"), { plan: "single", state: "new york" });
eq("slug state", C.planChoice("single", "north-carolina"), { plan: "single", state: "north carolina" });
eq("all states", C.planChoice("All 50 states ($999 a month)", ""), { plan: "all", state: "" });
eq("no state is refused", C.planChoice("single", ""), null);
eq("made-up plan is refused", C.planChoice("free", "Ohio"), null);
eq("prices", [C.PLANS.single.cents, C.PLANS.all.cents, C.PLANS.single.year_cents, C.PLANS.all.year_cents], [49900, 99900, 479040, 959040]);

// access follows the subscription
eq("active", C.accessFor("active"), "active");
eq("past due keeps access while Stripe retries", C.accessFor("past_due"), "active");
eq("canceled", C.accessFor("canceled"), "none");
eq("unpaid", C.accessFor("unpaid"), "none");
eq("incomplete", C.accessFor("incomplete"), "none");

// Stripe form encoding
eq("form", C.form({ mode: "subscription", line_items: [{ quantity: 1, price_data: { currency: "usd", recurring: { interval: "month" } } }], skip: undefined }),
  "mode=subscription&line_items%5B0%5D%5Bquantity%5D=1&line_items%5B0%5D%5Bprice_data%5D%5Bcurrency%5D=usd&line_items%5B0%5D%5Bprice_data%5D%5Brecurring%5D%5Binterval%5D=month");

// owners
eq("individual", C.ownerKind("GARNER, BEVERLY ANN"), "individual");
eq("company", C.ownerKind("OAK HOLDINGS LLC"), "company");
eq("government", C.ownerKind("CITY OF AUSTIN"), "government");
eq("state body", C.ownerKind("STATE OF FLORIDA DOT"), "government");
eq("unknown", C.ownerKind(""), "unknown");

// addresses: strict, null rather than a guess
eq("plain", C.parseAddress("123 Main St, Austin, TX 78701"), { line1: "123 MAIN ST", line2: "", city: "AUSTIN", state: "TX", zip: "78701" });
eq("care of and zip+4", C.parseAddress("C/O X, PO BOX 1, KANE, PA, 16735-1234"), { line1: "PO BOX 1", line2: "C/O X", city: "KANE", state: "PA", zip: "16735" });
eq("street and city in one piece", C.parseAddress("221 HOLYOKE RD BUTLER PA 16001"), { line1: "221 HOLYOKE RD", line2: "", city: "BUTLER", state: "PA", zip: "16001" });
eq("no zip", C.parseAddress("123 Main St, Austin, TX"), null);
eq("no street", C.parseAddress("Austin, TX 78701"), null);
eq("empty", C.parseAddress(null), null);

// postcard
const firm = { name: "Duerr Law, P.C.", phone: "555-0100", address_line1: "1 Main St", city: "Pittsburgh", state: "PA", zip: "15222" };
const lead = { id: 7, owner: "SMITH <b>JOHN</b>", mailing_address: "123 Main St, Austin, TX 78701", address: "123 MAIN ST", apn: "0042", parcel_county: "TRAVIS", project: "I-35 Widening (MPMS 123)", stage: "row_acquisition" };
const ok = C.postcardCheck(firm, {}, lead);
eq("ready to mail", [ok.blocker, ok.owner_kind, ok.to.city], [null, "individual", "AUSTIN"]);
eq("merge fields filled", [ok.front.includes("your property at 123 Main St"), ok.front.includes("I-35 Widening is in right-of-way acquisition"), ok.front.includes("(MPMS")], [true, true, false]);
eq("always labelled as advertising", ok.back.includes("ADVERTISEMENT. Duerr Law, P.C., 1 Main St, Pittsburgh, PA, 15222"), true);
eq("owner text cannot inject markup", C.postcardHtml(firm, { postcard_headline: "{{owner_name}} <script>" }, lead).front.includes("<script>"), false);
eq("unknown template keys are ignored", C.postcardHtml(firm, { fine: "x", postcard_back: "Call {{firm_phone}}" }, lead).back.includes("Call 555-0100"), true);
eq("public body is never mailed", C.postcardCheck(firm, {}, { ...lead, owner: "COUNTY OF TRAVIS" }).blocker, "This parcel is owned by a public body.");
eq("no owner", C.postcardCheck(firm, {}, { ...lead, owner: "" }).blocker, "No owner of record for this parcel.");
eq("unreadable address", C.postcardCheck(firm, {}, { ...lead, mailing_address: "somewhere" }).blocker.startsWith("The mailing address"), true);
eq("firm must have a return address", C.postcardCheck({ name: "X" }, {}, lead).blocker.startsWith("Add your firm"), true);

eq("an unknown stage is never printed as a stage", C.mergeValues(firm, { ...lead, stage: "unknown" }).project_stage, "the agency's published plans");
eq("nor is a missing one", C.mergeValues(firm, { ...lead, stage: undefined }).project_stage, "the agency's published plans");

// postcard credits
eq("packs", [C.creditPack("small").credits, C.creditPack("medium").cents, C.creditPack("large").credits], [25, 14000, 500]);
// every pack must clear the mail service's cost and the card processor's fee with at least 30 cents a card to spare
eq("no pack sells a card at a loss", Object.values(C.CREDIT_PACKS).every((p) => (p.cents - (p.cents * 0.029 + 30)) / p.credits - C.RATES.postcard.cost >= 30), true);
eq("an unknown pack is refused", [C.creditPack("free"), C.creditPack("constructor"), C.creditPack(undefined)], [null, null, null]);

// timing: a card goes out only when the owner has something to act on
eq("right-of-way acquisition can be mailed", [C.postcardCheck(firm, {}, lead).blocker, C.postcardCheck(firm, {}, lead).timing], [null, "now"]);
eq("design can be mailed", C.postcardCheck(firm, {}, { ...lead, stage: "design" }).blocker, null);
eq("planning is too early", C.postcardCheck(firm, {}, { ...lead, stage: "planning" }).blocker.startsWith("This project is still in planning"), true);
eq("construction is too late", C.postcardCheck(firm, {}, { ...lead, stage: "construction" }).blocker.includes("too late"), true);
eq("unknown timing warns but does not block", [C.postcardCheck(firm, {}, { ...lead, stage: "unknown" }).blocker, !!C.postcardCheck(firm, {}, { ...lead, stage: "unknown" }).warning], [null, true]);
eq("design more than five years out is too early", C.postcardCheck(firm, {}, { ...lead, stage: "design", window: "early" }).blocker.startsWith("This project is still"), true);

// skip tracing
eq("same parcel, same key", C.parcelKey({ state: "ca", county: " Los  Angeles ", apn: "12-3" }), "CA|LOS ANGELES|12-3");
eq("by parcel id", C.skipRequest({ state: "CA", county: "LOS ANGELES", apn: "12-3" }),
  { requests: [{ apn: "12-3", county: "Los Angeles", state: "CA" }], options: { includeTCPABlacklistedPhones: false } });
eq("by street address", C.skipRequest({ state: "VA", county: "FAIRFAX", address: "9 Oak Ln", city: "Vienna", zip: "22180" }).requests[0],
  { propertyAddress: { street: "9 Oak Ln", city: "Vienna", state: "VA", zip: "22180" } });
eq("nothing to search with", C.skipRequest({ state: "CA" }), null);
eq("no match", C.skipResult({ results: { persons: [] } }).matched, false);
const hit = C.skipResult({ results: { persons: [{ name: { first: "JANE", last: "DOE" }, mailingAddress: { street: "9 Oak Ln", city: "Vienna", state: "VA", zip: "22180" },
  phoneNumbers: [{ number: "5550001", score: 10 }, { number: "5550002", score: 90, dnc: true }], emails: [{ email: "j@x.com" }], litigator: false }] } });
eq("match", [hit.matched, hit.name, hit.mailing_address, hit.phones[0].number, hit.phones[0].dnc, hit.emails], [true, "Jane Doe", "9 OAK LN, VIENNA, VA, 22180", "5550002", true, ["j@x.com"]]);
eq("skip-traced address can be mailed", C.parseAddress(hit.mailing_address).zip, "22180");
eq("restricted states", C.RESTRICTED_STATES, ["ID", "MT", "SC", "WA"]);

// site visits
const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1";
const WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0";
eq("iphone", C.uaInfo(IPHONE), { device: "Phone", browser: "Safari", os: "iOS" });
eq("edge on windows", C.uaInfo(WIN), { device: "Computer", browser: "Edge", os: "Windows" });
eq("android tablet", C.uaInfo("Mozilla/5.0 (Linux; Android 13; SM-X700) AppleWebKit/537.36 Chrome/120 Safari/537.36").device, "Tablet");
eq("bots are not visitors", [C.isBot("Googlebot/2.1"), C.isBot(""), C.isBot("curl/8.1"), C.isBot(WIN)], [true, true, true, false]);
eq("server networks are not people", [C.isDatacenter("Amazon.com, Inc."), C.isDatacenter("Facebook, Inc."), C.isDatacenter("GTT Communications Inc."), C.isDatacenter("DigitalOcean, LLC")], [true, true, true, true]);
eq("internet providers are people", [C.isDatacenter("Comcast Cable Communications, LLC"), C.isDatacenter("Telmex Colombia S.A."), C.isDatacenter("Verizon Business"), C.isDatacenter(""), C.isDatacenter(null)], [false, false, false, false, false]);
eq("came from another site", C.refHost("https://www.google.com/search?q=x", "eminenttracker.com"), "google.com");
eq("came from our own site", C.refHost("https://eminenttracker.com/blog/", "eminenttracker.com"), "");
eq("no referrer", [C.refHost("", "eminenttracker.com"), C.refHost("not a url", "eminenttracker.com")], ["", ""]);
const seen = C.visitRow({ k: "view", vid: "abc12345def", sid: "s1", path: "/signup.html", ref: "https://www.linkedin.com/feed/", us: "apollo", uc: "launch", sw: 390, sh: 844, lang: "en-US", tz: "America/New_York", fresh: true, title: "Sign up" }, IPHONE, "eminenttracker.com", "v1");
eq("a visit", [seen.kind, seen.path, seen.ref_host, seen.utm_source, seen.utm_campaign, seen.device, seen.screen, seen.is_new, seen.event], ["view", "/signup.html", "linkedin.com", "apollo", "launch", "Phone", "390 x 844", true, null]);
eq("an event", C.visitRow({ k: "event", vid: "abc12345dee", path: "/", event: "Clicked sign up" }, WIN, "eminenttracker.com", "v1").event, "Clicked sign up");
eq("a bot is dropped", C.visitRow({ k: "view", vid: "abc12345def", path: "/" }, "Googlebot/2.1", "eminenttracker.com", "v1"), null);
eq("a bad path is dropped", C.visitRow({ k: "view", vid: "abc12345def", path: "javascript:alert(1)" }, WIN, "eminenttracker.com", "v1"), null);
eq("an event with no name is dropped", C.visitRow({ k: "event", vid: "abc12345def", path: "/" }, WIN, "eminenttracker.com", "v1"), null);
eq("long text is cut and control characters removed", C.clip("a\u0007b" + "x".repeat(50), 5), "a bxx");

// Stripe signature (needs Web Crypto)
if (typeof crypto !== "undefined" && crypto.subtle) {
  const body = '{"id":"evt_1"}', secret = "whsec_test", t = 1700000000;
  const sig = await C.hmacHex(secret, `${t}.${body}`);
  eq("good signature", await C.stripeSigned(body, `t=${t},v1=${sig}`, secret, t + 10), true);
  eq("changed body", await C.stripeSigned(body + " ", `t=${t},v1=${sig}`, secret, t + 10), false);
  eq("wrong secret", await C.stripeSigned(body, `t=${t},v1=${sig}`, "whsec_other", t + 10), false);
  eq("stale", await C.stripeSigned(body, `t=${t},v1=${sig}`, secret, t + 301), false);
  eq("missing header", await C.stripeSigned(body, null, secret, t), false);
  eq("known HMAC", await C.hmacHex("key", "The quick brown fox jumps over the lazy dog"), "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8");
} else say("(signature tests skipped: this engine has no Web Crypto)");

say(failed ? `${failed} of ${ran} FAILED` : `${ran} passed`);
if (failed) throw new Error("tests failed");
