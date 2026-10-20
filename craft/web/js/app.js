// Shell: navigation, routing, time range, command palette.
import { api, auth } from "./api.js";
import { h, clear, debounce, modal, toast, $ } from "./ui.js";

const ICONS = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  chart: '<path d="M4 19V5M4 19h16"/><path d="m7 15 4-5 3 3 5-7"/>',
  layout: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 9v12"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
  route: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H14a4 4 0 0 1 0 8H10a4 4 0 0 0 0 8"/>',
  alert: '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 9v5M12 17h.01"/>',
  bell: '<path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6Z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
  siren: '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M4 18h16v3H4z"/><path d="M12 3v2M5 6l1.5 1.5M19 6l-1.5 1.5"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14 0 18M12 3c-3 3.5-3 14 0 18"/>',
  gauge: '<path d="M4 18a8 8 0 1 1 16 0"/><path d="m12 14 4-5"/><circle cx="12" cy="14" r="1.4"/>',
  pipe: '<path d="M4 7h6v10h10"/><circle cx="4" cy="7" r="2"/><circle cx="20" cy="17" r="2"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  phone: '<path d="M5 3h4l2 5-3 2a12 12 0 0 0 6 6l2-3 5 2v4a2 2 0 0 1-2 2A17 17 0 0 1 3 5a2 2 0 0 1 2-2Z"/>',
  sliders: '<path d="M5 4v16M12 4v16M19 4v16"/><circle cx="5" cy="9" r="2"/><circle cx="12" cy="15" r="2"/><circle cx="19" cy="7" r="2"/>',
};

const NAV = [
  { group: "Observe" },
  { id: "overview", label: "Overview", path: "/", icon: "grid" },
  { id: "metrics", label: "Metrics", path: "/metrics", icon: "chart" },
  { id: "dashboards", label: "Dashboards", path: "/dashboards", icon: "layout" },
  { id: "logs", label: "Logs", path: "/logs", icon: "terminal" },
  { id: "search", label: "Search", path: "/search", icon: "search" },
  { id: "traces", label: "Traces", path: "/traces", icon: "route" },
  { id: "errors", label: "Errors", path: "/errors", icon: "alert" },
  { group: "Respond" },
  { id: "alerts", label: "Alerting", path: "/alerts", icon: "bell", badge: "alerts" },
  { id: "oncall", label: "On-call", path: "/oncall", icon: "phone" },
  { id: "incidents", label: "Incidents", path: "/incidents", icon: "siren", badge: "incidents" },
  { id: "slo", label: "SLOs", path: "/slo", icon: "target" },
  { id: "synthetics", label: "Synthetics", path: "/synthetics", icon: "globe" },
  { group: "Operate" },
  { id: "load", label: "Load tests", path: "/load", icon: "gauge" },
  { id: "collector", label: "Collector", path: "/collector", icon: "pipe" },
  { id: "settings", label: "Settings", path: "/settings", icon: "sliders" },
];

const RANGES = [
  { label: "5m", long: "Last 5 minutes", ms: 5 * 60e3 },
  { label: "15m", long: "Last 15 minutes", ms: 15 * 60e3 },
  { label: "1h", long: "Last hour", ms: 3600e3 },
  { label: "6h", long: "Last 6 hours", ms: 6 * 3600e3 },
  { label: "24h", long: "Last 24 hours", ms: 24 * 3600e3 },
  { label: "7d", long: "Last 7 days", ms: 7 * 86400e3 },
  { label: "30d", long: "Last 30 days", ms: 30 * 86400e3 },
];

const store = {
  range: localStorage.getItem("craft.range") || "6h",
  refresh: localStorage.getItem("craft.refresh") !== "off",
  badges: { alerts: 0, incidents: 0 },
  meta: null,
};

const state = { route: null, view: null, refreshTimer: null, cleanup: null, loading: false };

function rangeInfo(label = store.range) {
  const r = RANGES.find((x) => x.label === label) || RANGES[3];
  const endMs = Math.floor(Date.now() / 1000) * 1000;
  const startMs = endMs - r.ms;
  const targetPoints = 180;
  const step = Math.max(15, Math.round(r.ms / 1000 / targetPoints));
  return { ...r, startMs, endMs, start: Math.floor(startMs / 1000), end: Math.floor(endMs / 1000), step };
}

export const ctx = {
  api,
  state,
  get range() { return rangeInfo(); },
  rangeInfo,
  ranges: RANGES,
  setRange(label) {
    store.range = label;
    localStorage.setItem("craft.range", label);
    renderRangePicker();
    rerender();
  },
  setRefresh(on) {
    store.refresh = on;
    localStorage.setItem("craft.refresh", on ? "on" : "off");
    setupRefresh();
    const pill = $("#live-pill");
    pill.classList.toggle("paused", !on);
  },
  go(path) {
    if (location.hash === "#" + path) rerender();
    else location.hash = path;
  },
  open(path) { location.hash = path; },
  toast,
  refresh() { rerender(); },
  async refreshBadges() {
    try {
      const [alerts, incidents] = await Promise.all([api.alerts(), api.incidents("open")]);
      store.badges.alerts = alerts.alerts.filter((a) => a.state === "firing").length;
      store.badges.incidents = incidents.incidents.length;
      renderNav();
    } catch { /* shell stays usable offline */ }
  },
};

// ---------------- nav ----------------
function renderNav() {
  const nav = $("#rail-nav");
  clear(nav);
  for (const item of NAV) {
    if (item.group) { nav.appendChild(h("div", { class: "rail-group" }, item.group)); continue; }
    const active = state.route?.app === item.id;
    const count = item.badge ? store.badges[item.badge] : 0;
    const a = h("a", { class: `rail-link ${active ? "active" : ""}`, href: "#" + item.path, title: item.label },
      icon(item.icon),
      h("span", {}, item.label),
      count ? h("span", { class: `badge ${item.id === "alerts" ? "crit" : "warn"}` }, count) : null
    );
    nav.appendChild(a);
  }
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.innerHTML = ICONS[name] || ICONS.grid;
  return svg;
}

// ---------------- range picker ----------------
function renderRangePicker() {
  const wrap = $("#range-picker");
  clear(wrap);
  const sel = h("select", {
    class: "range-select", title: "Time range", value: store.range,
    onChange: (e) => ctx.setRange(e.target.value),
  }, RANGES.map((r) => h("option", { value: r.label, selected: r.label === store.range }, r.long)));
  wrap.appendChild(sel);
  const pill = $("#live-pill");
  pill.classList.toggle("paused", !store.refresh);
  pill.title = store.refresh ? "Auto-refresh every 30s (click to pause)" : "Paused (click to resume)";
  pill.onclick = () => ctx.setRefresh(!store.refresh);
}

function setupRefresh() {
  clearInterval(state.refreshTimer);
  if (!store.refresh) return;
  state.refreshTimer = setInterval(() => {
    if (document.hidden) return;
    if (document.querySelector(".modal-backdrop")) return;
    rerender(true);
  }, 30000);
}

// ---------------- router ----------------
function parseRoute() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [pathPart, queryPart] = raw.split("?");
  const params = new URLSearchParams(queryPart || "");
  const parts = pathPart.split("/").filter(Boolean);
  const app = parts[0] || "overview";
  return { app, parts, params, path: pathPart };
}

const TITLES = {
  overview: { title: "Overview", root: "TelemetryCraft" },
  metrics: { title: "Metrics", root: "Explore" },
  dashboards: { title: "Dashboards", root: "Explore" },
  logs: { title: "Logs", root: "Explore" },
  search: { title: "Search", root: "Explore" },
  traces: { title: "Traces", root: "Explore" },
  errors: { title: "Errors", root: "Explore" },
  alerts: { title: "Alerting", root: "Respond" },
  oncall: { title: "On-call", root: "Respond" },
  incidents: { title: "Incidents", root: "Respond" },
  slo: { title: "SLOs", root: "Respond" },
  synthetics: { title: "Synthetics", root: "Respond" },
  load: { title: "Load tests", root: "Operate" },
  collector: { title: "Collector", root: "Operate" },
  settings: { title: "Settings", root: "Operate" },
};

function renderCrumbs(route, extra) {
  const crumbs = $("#crumbs");
  clear(crumbs);
  const t = TITLES[route.app] || { title: route.app, root: "" };
  crumbs.appendChild(h("span", { class: "c-root" }, t.root));
  crumbs.appendChild(h("span", { class: "c-sep" }, "/"));
  crumbs.appendChild(h("strong", {}, extra || t.title));
  document.title = `${extra || t.title} · TelemetryCraft`;
}

let renderToken = 0;
async function rerender(isAuto = false) {
  const token = ++renderToken;
  const route = parseRoute();
  state.route = route;
  renderNav();
  renderCrumbs(route);
  const view = $("#view");
  if (!isAuto) {
    clear(view);
    view.appendChild(h("div", { class: "view-inner" }, h("div", { class: "loading" }, h("div", { class: "spinner" }))));
  }
  const container = isAuto ? view.querySelector(".view-inner") : null;
  try {
    const mod = await import(`./apps/${route.app}.js`);
    if (token !== renderToken) return;
    if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }
    const target = isAuto && container ? container : h("div", { class: "view-inner" });
    if (!isAuto) { clear(view); view.appendChild(target); }
    if (isAuto && container) clear(container);
    const cleanup = await mod.render(target, {
      ...ctx,
      route,
      setTitle: (title) => renderCrumbs(route, title),
      setCleanup: (fn) => { state.cleanup = fn; },
    });
    if (typeof cleanup === "function") state.cleanup = cleanup;
  } catch (err) {
    if (token !== renderToken) return;
    console.error(err);
    const parent = isAuto && container ? container : view;
    if (!isAuto) { clear(view); }
    (isAuto ? parent : view).appendChild(
      h("div", { class: "empty" },
        h("strong", {}, "This view failed to load"),
        h("div", { class: "mono", style: { marginTop: "6px" } }, String(err.message || err)),
        h("div", { class: "mt14" }, h("button", { class: "btn", onClick: () => rerender() }, "Retry")))
    );
  }
  ctx.refreshBadges();
}

// ---------------- command palette ----------------
function openPalette() {
  const results = h("div", { class: "palette-results" });
  const input = h("input", { placeholder: "Jump to a view, search logs, traces, issues, alerts…", autocomplete: "off" });
  const box = h("div", { class: "palette" }, input, results);
  const backdrop = h("div", { class: "modal-backdrop" }, box);
  let active = 0;
  let items = [];

  const draw = () => {
    clear(results);
    const sections = {};
    items.forEach((item, i) => {
      const key = item.section || "Go to";
      if (!sections[key]) { sections[key] = true; results.appendChild(h("div", { class: "palette-section" }, key)); }
      const row = h("div", { class: `palette-item ${i === active ? "active" : ""}`, onClick: () => choose(item) },
        h("span", {}, item.label),
        h("span", { class: "pi-type" }, item.type || ""));
      results.appendChild(row);
    });
    if (!items.length) results.appendChild(h("div", { class: "empty", style: { padding: "18px" } }, "No matches"));
  };

  const choose = (item) => { backdrop.remove(); document.removeEventListener("keydown", onKey); item.action(); };

  const base = () => NAV.filter((n) => !n.group).map((n) => ({ label: n.label, section: "Go to", type: "view", action: () => ctx.go(n.path) }));

  const search = debounce(async (term) => {
    items = [...base().filter((b) => !term || b.label.toLowerCase().includes(term.toLowerCase()))];
    if (term.length >= 2) {
      try {
        const res = await api.search(term);
        for (const issue of res.issues) items.push({ label: issue.title, section: "Issues", type: "error", action: () => ctx.go(`/errors/${issue.id}`) });
        for (const alert of res.alerts) items.push({ label: alert.name, section: "Alerts", type: "alert", action: () => ctx.go("/alerts") });
        for (const log of res.logs.slice(0, 4)) items.push({ label: log.body.slice(0, 90), section: "Logs", type: log.service, action: () => ctx.go(`/logs?query=${encodeURIComponent(`{service="${log.service}"}`)}`) });
        for (const trace of res.traces.slice(0, 4)) items.push({ label: `${trace.name} · ${Math.round(trace.duration_us / 1000)}ms`, section: "Traces", type: trace.service, action: () => ctx.go(`/traces/${trace.trace_id}`) });
        for (const dash of res.dashboards) items.push({ label: dash.title, section: "Dashboards", type: "dashboard", action: () => ctx.go(`/dashboards/${dash.uid}`) });
      } catch { /* keep local results */ }
    }
    active = 0;
    draw();
  }, 180);

  const onKey = (e) => {
    if (e.key === "Escape") { backdrop.remove(); document.removeEventListener("keydown", onKey); }
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(items.length - 1, active + 1); draw(); }
    if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); draw(); }
    if (e.key === "Enter" && items[active]) { e.preventDefault(); choose(items[active]); }
  };
  input.addEventListener("input", () => search(input.value));
  input.addEventListener("keydown", onKey);
  document.addEventListener("keydown", onKey);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) backdrop.remove(); });
  document.getElementById("modal-root").appendChild(backdrop);
  items = base();
  draw();
  input.focus();
}

function showShortcuts() {
  const rows = [
    ["⌘ K / Ctrl K", "Command palette"],
    ["R", "Refresh the current view"],
    ["[ / ]", "Previous / next time range"],
    ["?", "This dialog"],
    ["Esc", "Close dialogs"],
  ];
  modal({
    title: "Keyboard shortcuts",
    body: h("div", { class: "stack" }, rows.map(([k, v]) => h("div", { class: "row", style: { justifyContent: "space-between" } }, h("span", { class: "sub" }, v), h("kbd", { class: "badge" }, k)))),
  });
}

// ---------------- boot ----------------
function handleGlobalKeys(e) {
  const tag = document.activeElement?.tagName;
  const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || document.activeElement?.isContentEditable;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  if (typing) return;
  if (e.key === "?") { e.preventDefault(); showShortcuts(); }
  if (e.key.toLowerCase() === "r" && !e.metaKey && !e.ctrlKey) { e.preventDefault(); rerender(); }
  if (e.key === "[") { const i = RANGES.findIndex((r) => r.label === store.range); ctx.setRange(RANGES[Math.max(0, i - 1)].label); }
  if (e.key === "]") { const i = RANGES.findIndex((r) => r.label === store.range); ctx.setRange(RANGES[Math.min(RANGES.length - 1, i + 1)].label); }
}

// ---------------- auth gate ----------------
function showLogin(status = {}) {
  let settled = false;
  const method = status.method === "basic" ? "basic" : "token";
  const input = h("input", {
    class: "input",
    type: method === "basic" ? "text" : "password",
    placeholder: method === "basic" ? "user:password" : "paste the access token",
    autofocus: true,
    "aria-label": method === "basic" ? "user:password" : "access token",
  });
  const submit = h("button", { class: "btn primary" }, "Sign in");
  const note = h("div", { class: "sub", style: { minHeight: "18px" } },
    method === "basic" ? "Basic auth is on: type user:password." : "This server requires a bearer token.");
  const send = async () => {
    if (settled) return;
    const raw = input.value.trim();
    if (!raw) return;
    submit.disabled = true;
    note.textContent = "Checking…";
    try {
      const body = method === "basic"
        ? { user: raw.split(":")[0], password: raw.split(":").slice(1).join(":") }
        : { token: raw };
      const res = await api.login(body);
      auth.set(res.credential);
      settled = true;
      overlay.remove();
      await boot();
    } catch (err) {
      note.textContent = err.status === 429 ? "Too many attempts. Wait a minute." : "That did not work. Try again.";
      submit.disabled = false;
    }
  };
  submit.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  const overlay = h("div", { class: "auth-overlay", role: "dialog", "aria-modal": "true", "aria-label": "Sign in" },
    h("form", { class: "auth-card", onsubmit: (e) => { e.preventDefault(); send(); } },
      h("div", { class: "auth-brand" }, "Telemetry", h("span", {}, "Craft")),
      h("div", { class: "sub" }, "Local-first observability. Sign in to continue."),
      input,
      submit,
      note,
      h("div", { class: "sub", style: { fontSize: "11px" } }, `method: ${method} · credential is stored in this browser only`)));
  document.body.appendChild(overlay);
  setTimeout(() => input.focus(), 0);
}

let chromeReady = false;

function bootChrome() {
  renderRangePicker();
  renderNav();
  setupRefresh();
  $("#search-trigger").addEventListener("click", openPalette);
  $("#refresh-btn").addEventListener("click", () => { rerender(); toast("Refreshed"); });
  $("#rail-help").addEventListener("click", showShortcuts);
  document.addEventListener("keydown", handleGlobalKeys);
  window.addEventListener("hashchange", () => rerender());
  window.addEventListener("craft:auth-required", () => {
    const status = ctx.authStatus || { enabled: false, method: "token" };
    auth.clear();
    if (status.enabled && !$(".auth-overlay")) showLogin(status);
  });
  chromeReady = true;
}

async function boot() {
  const status = await api.authStatus().catch(() => ({ enabled: false }));
  ctx.authStatus = status;
  if (status.enabled && (!auth.credential() || status.ok === false)) {
    if (status.ok === false) auth.clear();
    showLogin(status);
    return;
  }
  if (!chromeReady) bootChrome();
  try {
    store.meta = await api.meta();
    const brand = $(".brand");
    if (!$(".brand-word")) brand.appendChild(h("span", { class: "brand-word" }, "Telemetry", h("span", {}, "Craft")));
    ctx.meta = store.meta;
  } catch (err) {
    console.warn("meta unavailable", err);
  }
  await rerender();
  ctx.refreshBadges();
}

boot();
