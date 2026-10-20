// Tiny DOM + formatting toolkit. No framework, no build step.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------- formatting ----------------
export function num(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (abs >= 1e4) return (v / 1e3).toFixed(1) + "k";
  if (abs >= 1000) return Math.round(v).toLocaleString();
  if (abs >= 100) return v.toFixed(0);
  if (abs >= 10) return v.toFixed(1);
  if (abs >= 1) return v.toFixed(digits);
  if (abs === 0) return "0";
  if (abs < 0.001) return v.toExponential(1);
  return v.toFixed(Math.min(4, digits + 2));
}

export function byUnit(v, unit) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  switch (unit) {
    case "percentunit": return (v * 100).toFixed(2) + "%";
    case "percent": return v.toFixed(1) + "%";
    case "s": return v < 1 ? Math.round(v * 1000) + "ms" : v.toFixed(2) + "s";
    case "ms": return v < 1000 ? v.toFixed(1) + "ms" : (v / 1000).toFixed(2) + "s";
    case "bytes": return bytes(v);
    case "reqps": return num(v) + "/s";
    case "currencyUSD": return "$" + num(v);
    case "short": return num(v);
    default: return num(v);
  }
}

export function bytes(v) {
  if (v === null || v === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  let n = Math.abs(v);
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}

export function ago(ts) {
  if (!ts) return "—";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - (ts > 1e11 ? ts / 1000 : ts)));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function dur(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

export function clock(ts, withDate = false) {
  const d = new Date((ts > 1e11 ? ts : ts * 1000));
  const opts = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
  if (withDate) { opts.month = "short"; opts.day = "2-digit"; }
  return d.toLocaleTimeString("en-GB", opts);
}

export function dayLabel(ts) {
  return new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export const initials = (name = "") => name.split(/\s+/).filter(Boolean).map((w) => w[0]).join("").slice(0, 2).toUpperCase() || "??";

// ---------------- components ----------------
export function badge(text, tone = "", extra = "") {
  return h("span", { class: `badge ${tone} ${extra}`.trim() }, text);
}

export function levelBadge(level) {
  const l = (level || "info").toLowerCase();
  const tone = l === "error" || l === "fatal" || l === "critical" ? "crit" : l === "warn" || l === "warning" ? "warn" : l === "debug" ? "muted" : "info";
  return h("span", { class: `badge ${tone}` }, l.toUpperCase());
}

export function sevBadge(sev) {
  return h("span", { class: `sev ${sev || "SEV3"}` }, sev || "SEV3");
}

export function stateBadge(state) {
  const tone = { firing: "crit", pending: "warn", resolved: "ok", acked: "info", healthy: "ok", breached: "crit", operational: "ok", degraded: "warn", partial_outage: "crit", investigating: "crit", identified: "warn", monitoring: "info" }[state] || "muted";
  return h("span", { class: `badge ${tone}` }, (state || "unknown").replace(/_/g, " "));
}

export function kpi(label, value, sub = "", opts = {}) {
  return h(
    "div",
    { class: `kpi ${opts.tone || ""} ${opts.onClick ? "clickable" : ""}`, onClick: opts.onClick, title: opts.title || "" },
    h("div", { class: "k-label" }, label),
    h("div", { class: "k-value" }, value, opts.suffix ? h("small", {}, opts.suffix) : null),
    sub ? h("div", { class: "k-sub" }, sub) : null
  );
}

export function panel(title, body, opts = {}) {
  const head = title
    ? h("div", { class: "panel-head" }, h("h3", {}, title), opts.meta ? h("span", { class: "muted" }, opts.meta) : null, opts.actions || null)
    : null;
  const wrap = h("div", { class: `panel ${opts.class || ""}`.trim() }, head, h("div", { class: opts.pad === false ? "" : "panel-body" }, body));
  return wrap;
}

export function empty(title, sub = "", action = null) {
  return h("div", { class: "empty" }, h("strong", {}, title), sub ? h("div", {}, sub) : null, action ? h("div", { class: "mt14" }, action) : null);
}

export function loading() {
  return h("div", { class: "loading" }, h("div", { class: "spinner" }));
}

export function table(columns, rows, opts = {}) {
  const thead = h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.align === "right" ? "right" : "" }, c.label))));
  const tbody = h("tbody", {}, rows.map((row) => {
    const tr = h("tr", { class: opts.onRow ? "clickable" : "" }, columns.map((c) => {
      const val = typeof c.render === "function" ? c.render(row) : row[c.key];
      return h("td", { class: `${c.align === "right" ? "num" : ""} ${c.class || ""}`.trim() }, val);
    }));
    if (opts.onRow) tr.addEventListener("click", () => opts.onRow(row));
    return tr;
  }));
  const t = h("table", { class: `data ${opts.class || ""}` }, thead, tbody);
  return opts.wrap === false ? t : h("div", { class: "table-wrap" }, t);
}

// ---------------- overlays ----------------
export function toast(message, kind = "") {
  const root = document.getElementById("toast-root");
  const node = h("div", { class: `toast ${kind}` }, message);
  root.appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

export function modal({ title, body, footer, wide = false, onClose }) {
  const root = document.getElementById("modal-root");
  const backdrop = h("div", { class: "modal-backdrop" });
  const close = () => { backdrop.remove(); document.removeEventListener("keydown", onKey); onClose?.(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const box = h(
    "div",
    { class: `modal ${wide ? "wide" : ""}` },
    h("div", { class: "modal-head" }, h("h2", {}, title), h("div", { class: "spacer" }), h("button", { class: "btn ghost sm", onClick: close }, "✕")),
    h("div", { class: "modal-body" }, body),
    footer ? h("div", { class: "modal-foot" }, footer) : null
  );
  backdrop.appendChild(box);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  document.addEventListener("keydown", onKey);
  root.appendChild(backdrop);
  setTimeout(() => box.querySelector("input,select,textarea,button")?.focus(), 30);
  return { close, box };
}

export function confirmDialog(title, message, onConfirm, confirmLabel = "Confirm", danger = false) {
  const m = modal({
    title,
    body: h("p", { class: "sub", style: { margin: 0 } }, message),
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: `btn ${danger ? "danger" : "primary"}`, onClick: () => { m.close(); onConfirm(); } }, confirmLabel),
    ],
  });
  return m;
}

export async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied to clipboard"); }
  catch { toast("Copy failed", "err"); }
}

export function copyButton(text) {
  return h("button", { class: "btn sm ghost copy", onClick: (e) => { e.stopPropagation(); copy(text); } }, "copy");
}

export function debounce(fn, ms = 220) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

export function tabs(items, active, onSelect) {
  return h("div", { class: "tabs" }, items.map((item) => {
    const id = typeof item === "string" ? item : item.id;
    const label = typeof item === "string" ? item : item.label;
    return h("button", { class: `tab ${id === active ? "active" : ""}`, onClick: () => onSelect(id) }, label);
  }));
}

export function formField(label, input, hint) {
  return h("div", { class: "field" }, h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null);
}

export function statBlock(label, value, sub) {
  return h("div", {}, h("div", { class: "k-label", style: { fontSize: "10px", color: "var(--text-3)", textTransform: "uppercase", letterSpacing: "0.06em" } }, label), h("div", { class: "num", style: { fontSize: "15px", marginTop: "3px" } }, value), sub ? h("div", { class: "hint" }, sub) : null);
}

export const COLORS = ["#9ae66e", "#62a8ff", "#b79bff", "#ffb454", "#6fd3c7", "#ff8fa3", "#f5c451", "#7dd3fc", "#c4b5fd", "#fca5a5", "#86efac", "#fdba74"];
export const colorFor = (i) => COLORS[i % COLORS.length];
