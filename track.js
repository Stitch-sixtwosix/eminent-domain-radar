// Counts visits to the public pages for the site's own admin console: which page, where the visitor came from, what kind
// of device, how long they stayed. It goes to our own server only. No advertising network, no third party, and the
// visitor's IP address is not stored. Signed-in administrators are not counted.
(() => {
  const C = window.ET_AUTH || {};
  if (!C.url || /^(localhost|127\.|\[::1\]|.*\.local$)/.test(location.hostname) || navigator.webdriver) return;
  const store = (kind, key, make) => { try { let v = kind.getItem(key); if (!v && make) { v = make(); kind.setItem(key, v); } return v; } catch (e) { return make ? make() : null; } };
  if (store(localStorage, "et_staff")) return;
  const rand = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), b => (b % 36).toString(36)).join("");
  let fresh = false;
  const v = store(localStorage, "et_v", () => { fresh = true; return rand(); }), sid = store(sessionStorage, "et_s", rand), vid = rand();
  const url = C.url + "/functions/v1/track", q = new URLSearchParams(location.search);
  const send = body => {
    const text = JSON.stringify(Object.assign({v, sid, path: location.pathname}, body));
    // text/plain keeps this a "simple" request, so the browser sends it without a preflight and it survives the page closing
    if (!(navigator.sendBeacon && navigator.sendBeacon(url, new Blob([text], {type: "text/plain"}))))
      fetch(url, {method: "POST", body: text, headers: {"Content-Type": "text/plain"}, keepalive: true}).catch(() => {});
  };
  send({k: "view", vid, fresh, title: document.title, ref: document.referrer, us: q.get("utm_source"), um: q.get("utm_medium"), uc: q.get("utm_campaign"),
        ut: q.get("utm_term"), ux: q.get("utm_content"), lang: navigator.language, tz: (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone,
        sw: screen.width, sh: screen.height});

  // time actually spent looking at the page, and how far down it was read
  let shown = document.visibilityState === "visible" ? Date.now() : 0, total = 0, deepest = 0, sent = -1;
  const depth = () => { const h = document.documentElement.scrollHeight - innerHeight; deepest = Math.max(deepest, h > 0 ? Math.round(100 * scrollY / h) : 100); };
  addEventListener("scroll", depth, {passive: true}); depth();
  const leave = () => {
    if (shown) { total += Date.now() - shown; shown = 0; }
    const secs = Math.round(total / 1000);
    if (secs !== sent && secs > 0) { sent = secs; send({k: "ping", vid, secs, scroll: deepest}); }
  };
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") leave(); else shown = Date.now(); });
  addEventListener("pagehide", leave);

  // the clicks that show intent
  document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a, button"); if (!a) return;
    const href = a.getAttribute("href") || "";
    const name = a.dataset.track || (/signup\.html/.test(href) ? "Clicked a sign-up link" : /login\.html/.test(href) ? "Clicked sign in"
      : /^mailto:/.test(href) ? "Clicked the email address" : a.id === "pay" ? "Clicked pay" : a.id === "google" ? "Chose Google sign-in"
      : a.type === "submit" && a.closest("#request") ? "Submitted the sign-up form" : a.type === "submit" && a.closest("#case") ? "Sent a support question" : "");
    if (name) send({k: "event", vid: rand(), event: name});
  });
})();
