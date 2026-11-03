/*! FastIdentity first-party collector. Consent-first: nothing is stored or sent
 *  until the CMP grants "analytics". Device signals are only sent with
 *  "marketing"/"advertisement" consent and are hashed server-side.
 *
 *  <script>
 *    window.FastIdentityConfig = { key: "wk_...", host: "https://t.example.com", cmp: "auto" };
 *  </script>
 *  <script async src="https://t.example.com/static/fi.js"></script>
 *
 *  cmp: "auto" (CookieYes / OneTrust / Google Consent Mode detection) | "manual"
 *  Manual: fastidentity.consent({ analytics: true, marketing: false })
 */
(function (w, d) {
  "use strict";
  var cfg = w.FastIdentityConfig || {};
  if (!cfg.key) return;
  var host = (cfg.host || "").replace(/\/$/, "");
  var COOKIE = "_fi_aid";
  var consent = null; // {analytics:bool, marketing:bool, functional:bool}
  var queue = [];
  var started = Date.now();
  var formsStarted = {};

  // ---------- consent detection ----------
  function fromCookieYes() {
    if (typeof w.getCkyConsent !== "function") return null;
    var c = w.getCkyConsent();
    if (!c || !c.isUserActionCompleted) return null;
    var cat = c.categories || {};
    return { analytics: !!cat.analytics, marketing: !!cat.advertisement, functional: !!cat.functional };
  }
  function fromOneTrust() {
    var g = w.OnetrustActiveGroups;
    if (typeof g !== "string" || !w.OneTrust || !w.OneTrust.IsAlertBoxClosed || !w.OneTrust.IsAlertBoxClosed()) return null;
    return { analytics: g.indexOf(",C0002,") > -1, functional: g.indexOf(",C0003,") > -1, marketing: g.indexOf(",C0004,") > -1 };
  }
  function fromGcm() {
    // Google Consent Mode v2 state pushed by most CMPs into dataLayer.
    var dl = w.dataLayer || [], state = null;
    for (var i = 0; i < dl.length; i++) {
      var e = dl[i];
      if (e && e[0] === "consent" && (e[1] === "update") && e[2]) state = Object.assign(state || {}, e[2]);
    }
    if (!state) return null;
    return { analytics: state.analytics_storage === "granted", marketing: state.ad_storage === "granted",
             functional: state.personalization_storage === "granted" };
  }
  function detect() {
    if (cfg.cmp === "manual") return consent;
    return fromCookieYes() || fromOneTrust() || fromGcm() || consent;
  }

  // ---------- storage (only after analytics consent) ----------
  function getCookie(n) {
    var m = d.cookie.match(new RegExp("(?:^|; )" + n + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : null;
  }
  function uuid() {
    if (w.crypto && w.crypto.randomUUID) return w.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0; return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }
  function anonId() {
    var id = getCookie(COOKIE);
    try { id = id || w.localStorage.getItem(COOKIE); } catch (e) {}
    if (!id) id = uuid();
    var secure = location.protocol === "https:" ? "; Secure" : "";
    d.cookie = COOKIE + "=" + id + "; Max-Age=31536000; Path=/; SameSite=Lax" + secure;
    try { w.localStorage.setItem(COOKIE, id); } catch (e) {}
    return id;
  }
  function forget() {
    d.cookie = COOKIE + "=; Max-Age=0; Path=/";
    try { w.localStorage.removeItem(COOKIE); } catch (e) {}
  }

  // ---------- context ----------
  function utm() {
    var p = new URLSearchParams(location.search), out = {};
    ["source", "medium", "campaign", "term", "content"].forEach(function (k) {
      var v = p.get("utm_" + k); if (v) out[k] = v;
    });
    return out;
  }
  function context() {
    var ctx = {
      consent: consent,
      page: { path: location.pathname, url: location.href, title: d.title, referrer: d.referrer },
      campaign: utm(),
      library: { name: "fi.js", version: "0.1.0" },
    };
    if (consent && consent.marketing) {
      // Raw signals are hashed with a server secret on arrival and never stored.
      ctx.userAgent = navigator.userAgent;
      ctx.screen = { width: screen.width, height: screen.height, density: w.devicePixelRatio || 1 };
      ctx.timezone = (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone;
      ctx.locale = navigator.language;
      ctx.device = { platform: navigator.platform };
    }
    return ctx;
  }

  // ---------- transport ----------
  function send(msg) {
    msg.writeKey = cfg.key;
    msg.messageId = msg.messageId || uuid();
    msg.timestamp = new Date().toISOString();
    msg.anonymousId = anonId();
    msg.context = Object.assign(context(), msg.context || {});
    var body = JSON.stringify(msg);
    // text/plain keeps this a CORS "simple request" (no preflight).
    try {
      fetch(host + "/collect", { method: "POST", body: body, keepalive: true, credentials: "omit",
                                 headers: { "content-type": "text/plain" } });
    } catch (e) {
      if (navigator.sendBeacon) navigator.sendBeacon(host + "/collect", body);
    }
  }
  function enqueue(msg) {
    if (consent === null) { queue.push(msg); return; }
    if (!consent.analytics && msg.type !== "consent") return; // drop silently
    send(msg);
  }
  function flush() {
    var q = queue; queue = [];
    q.forEach(enqueue);
  }
  function setConsent(c) {
    var prev = consent;
    consent = { analytics: !!c.analytics, marketing: !!c.marketing, functional: !!c.functional };
    if (!consent.analytics) {
      queue = [];
      // Record the withdrawal against the existing id, then delete it.
      if (prev && prev.analytics) { send({ type: "consent" }); forget(); }
      return;
    }
    if (!prev || prev.analytics !== consent.analytics || prev.marketing !== consent.marketing) {
      send({ type: "consent" });
    }
    flush();
  }

  // ---------- auto-capture ----------
  function onFocus(e) {
    var f = e.target && e.target.form;
    if (!f) return;
    var id = f.getAttribute("data-fi-form") || f.id || f.getAttribute("name") || f.action || "form";
    if (formsStarted[id]) return;
    formsStarted[id] = true;
    enqueue({ type: "track", event: "form_started", properties: { form: id } });
  }
  function onClick(e) {
    var el = e.target && e.target.closest && e.target.closest("[data-fi-event]");
    if (!el) return;
    var props = {};
    Array.prototype.forEach.call(el.attributes, function (a) {
      if (a.name.indexOf("data-fi-prop-") === 0) props[a.name.slice(13)] = a.value;
    });
    enqueue({ type: "track", event: el.getAttribute("data-fi-event"), properties: props });
  }
  function onHide() {
    if (d.visibilityState !== "hidden") return;
    var secs = Math.round((Date.now() - started) / 1000);
    if (secs >= 5) enqueue({ type: "track", event: "page_engaged", properties: { seconds: secs, path: location.pathname } });
    started = Date.now();
  }

  // ---------- public API ----------
  var api = {
    page: function (name, props) { enqueue({ type: "page", name: name || d.title, properties: props || {} }); },
    track: function (event, props) { enqueue({ type: "track", event: event, properties: props || {} }); },
    // traits may include email/phone. opts.consent records explicit opt-ins, e.g.
    // { marketing_email: { granted: true, wording: "Email me offers" } }
    identify: function (userId, traits, opts) {
      var msg = { type: "identify", traits: traits || {} };
      if (userId) msg.userId = userId;
      if (opts && opts.consent) msg.consent = opts.consent;
      enqueue(msg);
    },
    consent: setConsent,
    reset: forget,
  };
  var pending = w.fastidentity && w.fastidentity.q;
  w.fastidentity = api;

  d.addEventListener("focusin", onFocus, true);
  d.addEventListener("click", onClick, true);
  d.addEventListener("visibilitychange", onHide);
  w.addEventListener("cookieyes_consent_update", function () { var c = fromCookieYes(); if (c) setConsent(c); });
  w.addEventListener("OneTrustGroupsUpdated", function () { var c = fromOneTrust(); if (c) setConsent(c); });

  api.page();
  if (pending) pending.forEach(function (args) { api[args[0]].apply(api, args[1]); });
  var c0 = detect();
  if (c0) setConsent(c0);
  else if (cfg.cmp !== "manual") {
    // CMP may load after us: poll briefly.
    var tries = 0, t = setInterval(function () {
      var c = detect();
      if (c || ++tries > 40) { clearInterval(t); if (c) setConsent(c); }
    }, 250);
  }
})(window, document);
