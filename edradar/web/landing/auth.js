// Accounts: email and password or Google, through Supabase. The project URL and public key arrive in auth-config.js,
// written at publish time from config.yaml. With no project configured the site falls back to an emailed request.
const CFG = window.ET_AUTH || {};
export const authOn = !!(CFG.url && CFG.key);
if (authOn) document.documentElement.classList.add("auth-on");
if (authOn && CFG.google) document.documentElement.classList.add("google-on");   // shown once the Google provider is enabled
let client = null;
export async function supa() {
  if (!authOn) return null;
  if (!client) {
    const {createClient} = await import("https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.57.4/+esm");
    client = createClient(CFG.url, CFG.key);
  }
  return client;
}
export const here = name => new URL(name, location.href).href;        // a sibling page, as an absolute address
export async function google() {
  const s = await supa();
  const {error} = await s.auth.signInWithOAuth({provider: "google", options: {redirectTo: here("account.html")}});
  if (error) throw error;
}
export async function currentUser() {
  const s = await supa();
  if (!s) return null;
  const {data} = await s.auth.getSession();
  return data.session ? data.session.user : null;
}
export const FIRM_FIELDS = ["name", "firm", "phone", "location", "website", "plan", "plan_state", "states", "side"];
export const firmOf = user => Object.fromEntries(FIRM_FIELDS.map(k => [k, (user.user_metadata || {})[k === "name" ? "full_name" : k] || (k === "name" ? (user.user_metadata || {}).name : "") || ""]));
export const firmData = d => ({full_name: d.name, firm: d.firm, phone: d.phone || "", location: d.location || "", website: d.website || "", plan: d.plan || "", plan_state: d.plan_state || "", states: d.states || "", side: d.side});
export function say(el, text, bad) { el.textContent = text; el.hidden = !text; el.classList.toggle("bad", !!bad); }
export function checkField(field) {
  const bad = !field.checkValidity();
  field.setAttribute("aria-invalid", String(bad));
  let msg = field.parentNode.querySelector(".err");
  if (bad && !msg) { msg = document.createElement("span"); msg.className = "err"; msg.id = "err-" + field.name; field.setAttribute("aria-describedby", msg.id); field.parentNode.appendChild(msg); }
  if (msg) { if (bad) msg.textContent = field.type === "checkbox" ? "Tick this box to continue." : field.validity.valueMissing ? "This is needed." : field.validity.tooShort ? "Use at least 8 characters." : "Enter a full email address, like name@firm.com."; else msg.remove(); }
  return !bad;
}
export function watch(form) {
  form.addEventListener("blur", e => { if (e.target.matches("input[required]") && e.target.value) checkField(e.target); }, true);
  form.addEventListener("input", e => { if (e.target.getAttribute("aria-invalid") === "true") checkField(e.target); });
}
export function valid(form) {
  const bad = [...form.querySelectorAll("[required]")].filter(f => !f.closest("[hidden]")).filter(f => !checkField(f));
  if (bad.length) bad[0].focus();
  return !bad.length;
}
