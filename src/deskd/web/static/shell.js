/* deskd console shell — the shared runtime every view loads (from <head>, so
 * the theme applies before first paint). One copy of everything the three
 * pages used to duplicate: nav, theme, fetch, formatters, empty states, the
 * auto-refresh loop. NO credentials ever live here: the supervisor access
 * code is the meetings page's concern and stays in sessionStorage there.
 */
window.Shell = (function () {
  "use strict";

  var THEME_KEY = "deskd.theme"; // "auto" | "light" | "dark"

  // --- theme (applied immediately: this file loads in <head>) ---------------
  function themePref() {
    try { return localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { return "auto"; }
  }
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }
  applyTheme(themePref());

  // --- favicon: inline SVG wordmark, no asset, no network -------------------
  var FAVICON = "data:image/svg+xml," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
    '<rect width="32" height="32" rx="7" fill="#2a6fd6"/>' +
    '<text x="16" y="23" font-family="system-ui,sans-serif" font-size="19" font-weight="700" text-anchor="middle" fill="#fff">d</text></svg>');
  (function () {
    var link = document.createElement("link");
    link.rel = "icon"; link.type = "image/svg+xml"; link.href = FAVICON;
    document.head.appendChild(link);
  })();

  // --- tiny DOM/string helpers ----------------------------------------------
  function $(s) { return document.querySelector(s); }
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  // Skip the DOM write when nothing changed — the generalization of the
  // sticky-scroll fix: an unchanged innerHTML write still destroys text
  // selection and (for scrollable panes) the reader's position.
  function setHTML(el, html) {
    if (!el) return false;
    if (el.__shellLast === html) return false;
    el.__shellLast = html; el.innerHTML = html; return true;
  }

  // --- formatters -------------------------------------------------------------
  function ago(sec) {
    if (sec == null || isNaN(sec)) return "—";
    sec = Math.abs(Math.round(sec));
    if (sec < 60) return sec + "s";
    if (sec < 3600) return Math.floor(sec / 60) + "m";
    if (sec < 86400) return Math.floor(sec / 3600) + "h";
    return Math.floor(sec / 86400) + "d";
  }
  // Duration, two units ("4m 12s"): latencies and SLAs read better with the
  // remainder than rounded to one unit.
  function dur(sec) {
    if (sec == null || isNaN(sec)) return "—";
    sec = Math.abs(Math.round(sec));
    if (sec < 60) return sec + "s";
    if (sec < 3600) { var m = Math.floor(sec / 60); return m + "m" + (sec % 60 ? " " + (sec % 60) + "s" : ""); }
    if (sec < 86400) { var h = Math.floor(sec / 3600); var mm = Math.floor((sec % 3600) / 60); return h + "h" + (mm ? " " + mm + "m" : ""); }
    var d = Math.floor(sec / 86400); var hh = Math.floor((sec % 86400) / 3600);
    return d + "d" + (hh ? " " + hh + "h" : "");
  }
  function exact(ts) {
    var dte = new Date(ts);
    if (isNaN(dte)) return String(ts);
    return dte.toLocaleString([], { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) + " · " + dte.toISOString();
  }
  // Every timestamp in the console goes through rel(): humanized text, the
  // exact local+ISO value on hover.
  function rel(ts) {
    if (!ts) return "—";
    var sec = (Date.now() - new Date(ts)) / 1000;
    var text = sec >= 0 ? ago(sec) + " ago" : "in " + ago(sec);
    return '<span class="ts" title="' + esc(exact(ts)) + '">' + text + "</span>";
  }

  // --- consistent status colors + humanized enums, one map for every view ----
  var STATE_CLS = {
    // deliveries / wake outcomes
    read: "ok", acked: "ok", sent: "ok", done: "ok", online: "ok", available: "ok",
    routed: "ok", delivered: "ok", present: "ok", active: "ok",
    notified: "warn", pending: "warn", queued: "warn", in_progress: "warn",
    suspect: "warn", waiting: "warn", draining: "warn", timeout: "warn",
    consensus: "warn", termination_pending: "warn",
    overdue: "bad", escalated: "bad", failed: "bad", blocked: "bad", stuck: "bad",
    dead: "bad", offline: "bad", error: "bad", unwired: "bad", paused: "bad",
    superseded: "", cancelled: "", closed: "", never: ""
  };
  function stateClass(s) { var c = STATE_CLS[s]; return c == null ? "" : c; }
  function stateBadge(s) { return '<span class="badge ' + stateClass(s) + '">' + esc(human(s)) + "</span>"; }

  var WORDS = {
    // wake reasons
    meeting_wake: "meeting needs them", stuck_delivery: "message past read SLA",
    urgent_task: "urgent task", idle_task: "idle queue", inbox: "inbox batch",
    owed_reply: "owed reply",
    // default ladder channels (host ladders fall back to the raw name)
    hook: "in-session hook", resume: "resume session", spawn: "spawn session",
    human: "human channel", supervisor_badge: "supervisor badge",
    // hook kinds
    at: "timer", interval: "interval", probe: "probe", cron: "cron",
    // misc states
    in_progress: "in progress", termination_pending: "ending (vote pending)",
    supervisor_badge_recycle: "badge recycle"
  };
  function human(v) {
    if (v == null || v === "") return "—";
    return WORDS[v] || String(v).replace(/_/g, " ");
  }

  // Designed empty state: one-liner + (optionally) the exact CLI command that
  // populates the panel — board.html's hook empty state was the model.
  function empty(text, cmd) {
    return '<div class="empty"><span>' + esc(text) + "</span>" +
      (cmd ? "<br><code>" + esc(cmd) + "</code>" : "") + "</div>";
  }

  // Event payloads: labeled fields, never a raw k=v/JSON dump.
  function payloadChips(p) {
    if (!p || typeof p !== "object") return "";
    var parts = [];
    Object.keys(p).forEach(function (k) {
      var v = p[k];
      if (v == null) return;
      if (typeof v === "object") v = Array.isArray(v) ? v.join(", ") : "…";
      parts.push('<span class="kvchip"><em>' + esc(human(k)) + "</em>" + esc(v) + "</span>");
    });
    return parts.length ? ' <span class="chips" style="display:inline-flex;vertical-align:middle">' + parts.join("") + "</span>" : "";
  }

  // --- fetch helper: one consistent error surface -----------------------------
  async function api(url, opts) {
    var r = await fetch(url, opts);
    var data = null;
    try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
    if (!r.ok) throw new Error((data && data.detail) || "HTTP " + r.status + " on " + url);
    if (data == null) throw new Error("response could not be parsed: " + url);
    return data;
  }

  // --- meta (project name, roles, auth mode) — fetched once, shared -----------
  var metaPromise = null;
  function meta() {
    if (!metaPromise) metaPromise = api("/api/meeting-meta");
    return metaPromise;
  }

  // --- nav shell ----------------------------------------------------------------
  var NAV = [
    ["board", "/", "Board"],
    ["office", "/office", "Office floor"],
    ["meetings", "/meetings", "Meetings"],
    ["wake", "/wake", "Wake ladder"],
    ["escalations", "/escalations", "Escalations"],
    ["tasks", "/tasks", "Tasks & hooks"]
  ];
  var LOGO = '<svg width="20" height="20" viewBox="0 0 32 32" aria-hidden="true">' +
    '<rect width="32" height="32" rx="7" fill="#2a6fd6"/>' +
    '<text x="16" y="23" font-family="system-ui,sans-serif" font-size="19" font-weight="700" text-anchor="middle" fill="#fff">d</text></svg>';

  function themeLabel() {
    return { auto: "◐ auto", light: "○ light", dark: "● dark" }[themePref()];
  }

  /** Render the top bar. `page` picks the active nav item; `title` is the
   *  view's name — the <title> becomes "Title · <project> console" once the
   *  project name arrives from /api/meeting-meta. */
  function init(page, title) {
    var bar = document.createElement("header");
    bar.className = "topbar";
    bar.innerHTML = '<div class="tb-inner">' +
      '<a class="brand" href="/">' + LOGO + '<span id="sh-project">deskd</span><span class="brand-sub">console</span></a>' +
      '<nav class="topnav">' + NAV.map(function (n) {
        return '<a href="' + n[1] + '"' + (n[0] === page ? ' class="active"' : "") + ">" + n[2] + "</a>";
      }).join("") + "</nav>" +
      '<div class="tb-right">' +
      '<span id="sh-refresh" class="tb-btn" hidden></span>' +
      '<span id="sh-auth" class="badge" title="how supervisor actions are authenticated">…</span>' +
      '<button id="sh-theme" class="tb-btn" title="theme: auto follows the system">' + themeLabel() + "</button>" +
      "</div></div>";
    document.body.insertBefore(bar, document.body.firstChild);

    $("#sh-theme").onclick = function () {
      var order = ["auto", "light", "dark"];
      var next = order[(order.indexOf(themePref()) + 1) % 3];
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode */ }
      applyTheme(next); $("#sh-theme").textContent = themeLabel();
    };

    meta().then(function (m) {
      var name = m.project || "deskd";
      $("#sh-project").textContent = name;
      if (title) document.title = title + " · " + name + " console";
      var el = $("#sh-auth"), mode = m.supervisor_auth_mode;
      if (mode === "open") { el.className = "badge bad"; el.textContent = "supervisor auth OFF"; }
      else if (m.signed_auth_enabled && m.simple_auth_enabled) { el.className = "badge ok"; el.textContent = "supervisor: code or signed"; }
      else if (m.signed_auth_enabled) { el.className = "badge ok"; el.textContent = "supervisor: signed"; }
      else if (m.simple_auth_enabled) { el.className = "badge ok"; el.textContent = "supervisor: access code"; }
      else { el.className = "badge"; el.textContent = "read-only"; }
    }).catch(function () {
      $("#sh-auth").textContent = "read-only";
    });
  }

  // --- auto-refresh with a visible countdown, paused while the reader hovers ---
  // Hovering the content means someone is READING it; yanking the DOM out from
  // under them is the old 5s-poll bug in new clothes. The countdown chip shows
  // the pause, and clicking it refreshes immediately.
  function autorefresh(fn, seconds) {
    var chip = $("#sh-refresh");
    var left = 0, paused = false, failed = false, running = false;
    function draw() {
      if (!chip) return;
      chip.hidden = false;
      chip.className = "tb-btn" + (failed ? " error" : paused ? " paused" : "");
      chip.title = failed ? "last refresh failed — click to retry" :
        paused ? "auto-refresh paused while you hover the page" :
        "auto-refresh; click to refresh now";
      chip.textContent = failed ? "↻ retry" : paused ? "↻ paused" : "↻ " + left + "s";
    }
    function run() {
      if (running) return;
      running = true;
      Promise.resolve().then(fn).then(function () { failed = false; },
        function () { failed = true; })
        .then(function () { running = false; left = seconds; draw(); });
    }
    var main = document.querySelector("main");
    if (main) {
      main.addEventListener("mouseenter", function () { paused = true; draw(); });
      main.addEventListener("mouseleave", function () { paused = false; draw(); });
    }
    if (chip) chip.onclick = function () { run(); };
    setInterval(function () {
      if (paused || running) return;
      left -= 1;
      if (left <= 0) run(); else draw();
    }, 1000);
    run();
  }

  return {
    $: $, esc: esc, setHTML: setHTML,
    ago: ago, dur: dur, rel: rel, exact: exact,
    stateClass: stateClass, stateBadge: stateBadge, human: human,
    empty: empty, payloadChips: payloadChips,
    api: api, meta: meta, init: init, autorefresh: autorefresh
  };
})();
