// Search — Kibana/OpenSearch Discover: index pattern, field sidebar, histogram, saved queries.
import { h, clear, panel, badge, levelBadge, empty, clock, toast, num, modal, formField, ago } from "../ui.js";
import { volumeBars } from "../charts.js";

const INDEX = "logs-*";

const SUGGESTED = [
  { label: "Everything", q: '{service=~".+"}' },
  { label: "Errors", q: '{service=~".+"} |= "error"' },
  { label: "5xx responses", q: '{service=~".+"} |~ "status=5[0-9][0-9]"' },
  { label: "Timeouts", q: '{service=~".+"} |~ "(?i)timeout|deadline|context canceled"' },
  { label: "Database", q: '{service=~".+"} |~ "(?i)deadlock|pq:|slow query"' },
];

function toLogQL(text) {
  const raw = (text || "").trim();
  if (!raw) return SUGGESTED[0].q;
  if (raw.startsWith("{")) return raw;
  // Lucene-ish free text: terms become case-insensitive line filters on the default stream
  const terms = raw.split(/\s+/).filter(Boolean).map((t) => `|~ "(?i)${t.replace(/["\\]/g, "")}"`);
  return `{service=~".+"} ${terms.join(" ")}`.trim();
}

export async function render(root, ctx) {
  ctx.setTitle("Search");
  let query = ctx.route.params.get("q") || localStorage.getItem("craft.searchQuery") || SUGGESTED[0].q;
  let entries = [];
  let saved = [];
  let level = "";

  const input = h("input", { class: "input mono q-input", value: query, spellcheck: "false", placeholder: "error checkout…" });
  const runBtn = h("button", { class: "btn primary" }, "Search");
  const saveBtn = h("button", { class: "btn ghost" }, "Save");
  const histHost = h("div", { class: "panel", style: { overflow: "hidden" } });
  const bodyHost = h("div", {});
  const savedHost = h("div", { class: "chips" });
  const fieldHost = h("div", {});

  root.appendChild(h("div", { class: "panel panel-pad", style: { marginBottom: "10px" } },
    h("div", { class: "row", style: { alignItems: "center", gap: "8px", flexWrap: "wrap" } },
      h("span", { class: "badge", title: "Index pattern" }, INDEX),
      input, runBtn, saveBtn),
    h("div", { class: "row", style: { marginTop: "8px", gap: "8px", flexWrap: "wrap", alignItems: "center" } },
      h("span", { class: "hint" }, "saved:"), savedHost,
      h("span", { class: "spacer" }),
      h("span", { class: "hint mono" }, "free text ⟶ {service=~\".+\"} |~ \"(?i)term\""))));

  root.appendChild(h("div", { class: "grid two", style: { marginBottom: "10px" } }, histHost, panel("Fields", fieldHost)));

  const layout = h("div", { class: "grid", style: { gridTemplateColumns: "minmax(0, 1fr)" } }, bodyHost);
  root.appendChild(layout);

  function apply() {
    query = toLogQL(input.value);
    localStorage.setItem("craft.searchQuery", query);
    run();
  }
  runBtn.onclick = apply;
  input.onkeydown = (e) => { if (e.key === "Enter") apply(); };
  saveBtn.onclick = () => {
    const nameIn = h("input", { class: "input", value: input.value.slice(0, 60) || "Saved search" });
    modal({
      title: "Save this search",
      body: formField("Name", nameIn, "Saved searches are shared by everyone using this instance."),
      footer: h("div", { class: "row", style: { justifyContent: "flex-end" } },
        h("button", { class: "btn primary", onClick: async () => {
          try {
            await ctx.api.saveSearch({ app: "search", name: nameIn.value.trim() || "Saved search", query });
            document.querySelector(".modal-backdrop")?.remove();
            toast("Search saved");
            await loadSaved();
          } catch { /* toast already shown */ }
        } }, "Save")),
    });
  };

  async function loadSaved() {
    try {
      const res = await ctx.api.searches();
      saved = (res.searches || []).filter((s) => s.app === "search");
    } catch { saved = []; }
    clear(savedHost);
    if (!saved.length) {
      SUGGESTED.forEach((s) => savedHost.appendChild(h("button", { class: "chip add", onClick: () => { input.value = s.q; apply(); } }, s.label)));
      return;
    }
    for (const s of saved) {
      savedHost.appendChild(h("button", { class: "chip add", title: s.query, onClick: () => { input.value = s.query; apply(); } },
        s.name,
        h("span", { class: "muted", style: { marginLeft: "6px" }, onClick: async (e) => {
          e.stopPropagation();
          await ctx.api.deleteSearch(s.id);
          loadSaved();
        } }, "×")));
    }
  }

  async function run() {
    clear(bodyHost);
    bodyHost.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
    try {
      const [res, vol, stats, fields] = await Promise.all([
        ctx.api.logs(query, ctx.range.start, ctx.range.end, 500),
        ctx.api.logVolume(query, ctx.range.start, ctx.range.end, Math.max(60, Math.round((ctx.range.end - ctx.range.start) / 60))),
        ctx.api.logStats(query, ctx.range.start, ctx.range.end),
        ctx.api.logFields(),
      ]);
      entries = res.entries || [];
      drawHistogram(vol, res.count);
      drawFields(fields, stats);
      drawResults(stats);
    } catch (err) {
      clear(bodyHost);
      bodyHost.appendChild(empty("Search failed", err.message));
    }
  }

  function drawHistogram(vol, count) {
    clear(histHost);
    const buckets = new Map();
    for (const s of vol.series || []) for (const [ts, v] of s.values) buckets.set(ts, (buckets.get(ts) || 0) + v);
    const arr = [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([ts, value]) => ({ ts: ts * 1000, value }));
    const host = h("div", { style: { padding: "10px 10px 0" } });
    histHost.appendChild(h("div", { class: "row", style: { padding: "9px 12px 0", alignItems: "center" } },
      h("strong", { style: { fontSize: "12px" } }, "Hits"), h("span", { class: "spacer" }),
      h("span", { class: "hint" }, `${num(count || 0)} lines · ${ctx.range.long}`)));
    histHost.appendChild(host);
    volumeBars(host, arr, { height: 84, colorFor: (b) => (b.value > 20 ? "#c96a5a" : "#4b8f5f") });
  }

  function drawFields(fields, stats) {
    clear(fieldHost);
    const section = (title, rows) => {
      fieldHost.appendChild(h("div", { class: "hint", style: { marginTop: "8px", marginBottom: "4px", textTransform: "uppercase", fontSize: "10px", letterSpacing: "0.06em" } }, title));
      if (!rows.length) return fieldHost.appendChild(h("div", { class: "hint" }, "nothing in range"));
      for (const [name, count] of rows) {
        fieldHost.appendChild(h("button", {
          class: "field-row",
          onClick: () => {
            input.value = `{service="${name}"}`;
            apply();
          },
        }, h("span", { class: "mono" }, name), h("span", { class: "muted" }, num(count))));
      }
    };
    section("service", Object.entries(stats.by_service || {}).slice(0, 6));
    section("level", Object.entries(stats.by_level || {}));
    const all = [...new Set([...(fields.services || []), ...(fields.levels || [])])];
    if (all.length) fieldHost.appendChild(h("div", { class: "hint", style: { marginTop: "10px" } }, `index has ${all.length} field values`));
  }

  function drawResults(stats) {
    clear(bodyHost);
    const filtered = level ? entries.filter((e) => e.level === level) : entries;
    const chips = ["", "error", "warn", "info", "debug"].map((lvl) =>
      h("button", {
        class: `chip add ${level === lvl ? "active" : ""}`,
        onClick: () => { level = lvl; drawResults(stats); },
      }, lvl || "all"));

    const rows = filtered.slice(0, 300).map((e) => ({ ...e }));
    const cols = [
      { label: "Time", class: "mono", render: (r) => clock(r.ts) },
      { label: "Level", render: (r) => levelBadge(r.level) },
      { label: "Service", render: (r) => h("span", { class: "mono" }, r.service || "—") },
      { label: "Message", render: (r) => h("div", { class: "truncate", style: { maxWidth: "760px" } }, r.body) },
    ];
    bodyHost.appendChild(panel("Results", h("div", {},
      h("div", { class: "row", style: { gap: "8px", marginBottom: "8px", flexWrap: "wrap" } },
        h("span", { class: "badge ok" }, `${filtered.length} hits`),
        h("span", { class: "hint" }, `matched ${num(stats.total || 0)} of the index`),
        h("span", { class: "spacer" }),
        h("div", { class: "chips" }, chips)),
      filtered.length
        ? tableOf(cols, rows, (r) => openDetail(r))
        : empty("No hits", "Try a broader query or a longer time range.")),
      { pad: false, meta: "click a row for the raw document" }));
  }

  function tableOf(cols, rows, onRow) {
    const thead = h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.align === "right" ? "right" : "" }, c.label))));
    const tbody = h("tbody", {}, rows.map((row) => {
      const tr = h("tr", { class: "clickable" }, cols.map((c) => h("td", { class: `${c.class || ""}`.trim() }, c.render ? c.render(row) : row[c.key])));
      tr.onclick = () => onRow(row);
      return tr;
    }));
    return h("div", { class: "table-wrap" }, h("table", { class: "data" }, thead, tbody));
  }

  function openDetail(row) {
    const doc = {
      _index: INDEX,
      _id: String(row.id ?? ""),
      _timestamp: new Date(row.ts * 1000).toISOString(),
      service: row.service,
      level: row.level,
      message: row.body,
      trace_id: row.trace_id,
      labels: row.labels || {},
      attributes: row.attrs || {},
    };
    modal({
      title: "Document",
      wide: true,
      body: h("div", {},
        h("div", { class: "row", style: { gap: "8px", marginBottom: "10px" } }, badge(row.level || "info"),
          h("span", { class: "mono" }, row.service || ""), h("span", { class: "spacer" }), h("span", { class: "hint" }, ago(row.ts))),
        h("div", { class: "mono", style: { whiteSpace: "pre-wrap", fontSize: "12px", lineHeight: "1.6", background: "#11141a", border: "1px solid #232833", borderRadius: "6px", padding: "12px", maxHeight: "52vh", overflow: "auto" } }, JSON.stringify(doc, null, 2))),
    });
  }

  await loadSaved();
  await run();
  ctx.setCleanup(() => {});
}
