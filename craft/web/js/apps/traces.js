import { h, clear, panel, badge, empty, clock, num, byUnit, tabs, toast, ago, colorFor } from "../ui.js";
import { scatter, timeSeries, mount, barList } from "../charts.js";

export async function render(root, ctx) {
  if (ctx.route.parts[1]) return renderTrace(root, ctx, ctx.route.parts[1]);
  ctx.setTitle("Traces");
  const r = ctx.range;
  let tab = "search";
  let filters = { service: "", status: "", operation: "", min_duration_ms: "", sort: "duration" };
  const resultsHost = h("div", {});
  const tabHost = h("div", {});
  const summaryHost = h("div", {});

  const serviceSelect = h("select", { class: "select" }, h("option", { value: "" }, "any service"));
  const statusSelect = h("select", { class: "select" },
    h("option", { value: "" }, "any status"), h("option", { value: "ok" }, "ok"), h("option", { value: "error" }, "error"));
  const opInput = h("input", { class: "input", placeholder: "operation contains…" });
  const durInput = h("input", { class: "input", placeholder: "min duration (ms)", type: "number" });
  const sortSelect = h("select", { class: "select" }, h("option", { value: "duration" }, "slowest first"), h("option", { value: "recent" }, "most recent"));
  const listHost = h("div", {});
  const scatterHost = h("div", {});

  async function search() {
    filters = {
      service: serviceSelect.value, status: statusSelect.value, operation: opInput.value,
      min_duration_ms: durInput.value, sort: sortSelect.value,
    };
    clear(resultsHost);
    resultsHost.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
    try {
      const res = await ctx.api.traces({ start: r.start, end: r.end, limit: 200, ...filters });
      clear(resultsHost);
      const traces = res.traces || [];
      clear(summaryHost);
      const errors = traces.filter((t) => t.status === "error").length;
      summaryHost.appendChild(h("div", { class: "kpi-grid mb14" },
        h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Traces"), h("div", { class: "k-value" }, num(traces.length))),
        h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Errors"), h("div", { class: "k-value", style: { color: errors ? "var(--red)" : "inherit" } }, errors)),
        h("div", { class: "kpi" }, h("div", { class: "k-label" }, "p95 duration"), h("div", { class: "k-value" }, byUnit(p95(traces), "ms"))),
        h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Services involved"), h("div", { class: "k-value" }, new Set(traces.map((t) => t.service)).size))));
      const sc = h("div", {});
      scatterHost.appendChild(sc);
      scatter(sc, traces, { height: 170, onSelect: (t) => ctx.go(`/traces/${t.trace_id}`) });
      const rows = traces.map((t) => h("tr", { class: "clickable", onClick: () => ctx.go(`/traces/${t.trace_id}`) },
        h("td", {}, h("div", { style: { fontWeight: 500 } }, t.root), h("div", { class: "hint mono" }, t.trace_id.slice(0, 16) + "…")),
        h("td", {}, h("span", { class: "badge muted" }, t.service)),
        h("td", { class: "num" }, byUnit(t.duration_ms, "ms")),
        h("td", { class: "num" }, t.spans),
        h("td", { class: "num" }, t.errors ? h("span", { class: "badge crit" }, t.errors) : h("span", { class: "muted" }, "0")),
        h("td", { class: "num muted" }, clock(t.start_us / 1e6, true))));
      resultsHost.appendChild(panel("", h("div", { class: "table-wrap", style: { maxHeight: "420px" } }, h("table", { class: "data trace-table" },
        h("thead", {}, h("tr", {}, ["Root operation", "Service", "Duration", "Spans", "Errors", "Started"].map((c, i) => h("th", { class: i > 1 && i < 5 ? "right" : "" }, c)))),
        h("tbody", {}, rows))), { pad: false }));
      if (!traces.length) resultsHost.appendChild(empty("No traces match", "Try clearing filters or widening the time range."));
    } catch (err) {
      clear(resultsHost);
      resultsHost.appendChild(empty("Search failed", err.message));
    }
  }

  const p95 = (list) => {
    const ds = list.map((t) => t.duration_ms).sort((a, b) => a - b);
    return ds.length ? ds[Math.floor(ds.length * 0.95)] : 0;
  };

  async function drawAggregates() {
    clear(resultsHost);
    resultsHost.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
    try {
      const [agg, graph] = await Promise.all([ctx.api.traceAggregates(r.start, r.end, "service"), ctx.api.serviceGraph(r.start, r.end)]);
      clear(resultsHost);
      resultsHost.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap" } },
        h("div", { style: { flex: "1 1 420px", minWidth: "320px" } }, panel("Service latency", h("div", {}, agg.groups.map((g) => {
          const el = h("div", {});
          mount(el, () => barList(el, [
            { label: "p50", value: g.p50_ms, color: "#3f6d8f" },
            { label: "p95", value: g.p95_ms, color: "#9ae66e" },
            { label: "p99", value: g.p99_ms, color: "#f5c451" },
          ], { format: (v) => byUnit(v, "ms") }));
          return h("div", { style: { marginBottom: "12px" } },
            h("div", { class: "row", style: { alignItems: "baseline" } }, h("strong", { style: { fontSize: "12.5px" } }, g.key), h("span", { class: "spacer" }), h("span", { class: "hint mono" }, `${num(g.count)} spans · ${g.error_rate}% err`)),
            el);
        }))), { pad: true })),
        h("div", { style: { flex: "1 1 420px", minWidth: "320px" } }, panel("Service map", serviceMap(graph), { pad: true })));

      resultsHost.appendChild(h("div", { class: "mt14" }, panel("Aggregates", h("table", { class: "data" },
        h("thead", {}, h("tr", {}, ["Service", "Spans", "Errors", "Error rate", "Avg", "p50", "p95", "p99", "Max"].map((c, i) => h("th", { class: i ? "right" : "" }, c)))),
        h("tbody", {}, agg.groups.map((g) => h("tr", {},
          h("td", {}, h("span", { class: "badge muted" }, g.key)),
          h("td", { class: "num" }, num(g.count)),
          h("td", { class: "num" }, g.errors),
          h("td", { class: "num" }, h("span", { class: `badge ${g.error_rate > 2 ? "crit" : g.error_rate > 0 ? "warn" : "ok"}` }, g.error_rate + "%")),
          h("td", { class: "num" }, byUnit(g.avg_ms, "ms")),
          h("td", { class: "num" }, byUnit(g.p50_ms, "ms")),
          h("td", { class: "num" }, byUnit(g.p95_ms, "ms")),
          h("td", { class: "num" }, byUnit(g.p99_ms, "ms")),
          h("td", { class: "num" }, byUnit(g.max_ms, "ms")))))), { pad: false })));
    } catch (err) {
      clear(resultsHost);
      resultsHost.appendChild(empty("Aggregates unavailable", err.message));
    }
  }

  function serviceMap(graph) {
    const nodes = graph.nodes || [];
    const edges = graph.edges || [];
    if (!nodes.length) return empty("No service edges", "Span parent/child relationships produce this graph.");
    const width = 520, height = 300;
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "svc-map");
    // simple radial layout with api-gateway at the centre-left
    const center = { x: width / 2, y: height / 2 };
    const positions = new Map();
    nodes.forEach((n, i) => {
      const angle = (i / nodes.length) * Math.PI * 2 - Math.PI / 2;
      const rad = n.name === "api-gateway" ? 0 : 105;
      positions.set(n.name, { x: center.x + Math.cos(angle) * rad, y: center.y + Math.sin(angle) * rad * 0.85 });
    });
    edges.forEach((e) => {
      const a = positions.get(e.source), b = positions.get(e.target);
      if (!a || !b) return;
      const line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", a.x); line.setAttribute("y1", a.y); line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
      line.setAttribute("class", "edge" + (e.errors ? " err" : ""));
      line.setAttribute(" stroke-width", String(Math.min(4, 1 + Math.log10(e.calls + 1))));
      svg.appendChild(line);
      const tx = (a.x + b.x) / 2, ty = (a.y + b.y) / 2;
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", tx); label.setAttribute("y", ty - 3);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("style", "font: 8.5px var(--mono); fill: #5f6a77");
      label.textContent = `${num(e.calls)} · ${e.avg_ms}ms`;
      svg.appendChild(label);
    });
    nodes.forEach((n) => {
      const p = positions.get(n.name);
      if (!p) return;
      const g = document.createElementNS(svgNS, "g");
      g.setAttribute("class", "svc-node");
      const w = Math.max(66, n.name.length * 5.4 + 16);
      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", p.x - w / 2); rect.setAttribute("y", p.y - 13);
      rect.setAttribute("width", w); rect.setAttribute("height", 26); rect.setAttribute("rx", 6);
      g.appendChild(rect);
      const t = document.createElementNS(svgNS, "text");
      t.setAttribute("x", p.x); t.setAttribute("y", p.y + 1);
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("dominant-baseline", "middle");
      t.textContent = n.name.length > 17 ? n.name.slice(0, 16) + "…" : n.name;
      g.appendChild(t);
      const sub = document.createElementNS(svgNS, "text");
      sub.setAttribute("x", p.x); sub.setAttribute("y", p.y + 24);
      sub.setAttribute("text-anchor", "middle");
      sub.setAttribute("class", "n-sub");
      sub.textContent = `${num(n.spans || 0)} spans${n.errors ? ` · ${n.errors} err` : ""}`;
      g.appendChild(sub);
      g.addEventListener("click", () => { serviceSelect.value = n.name; switchTab("search"); search(); });
      g.style.cursor = "pointer";
      svg.appendChild(g);
    });
    return svg;
  }

  async function drawVolume() {
    try {
      const vol = await ctx.api.traceVolume(r.start, r.end, Math.max(30, Math.round((r.end - r.start) / 90)));
      const host = h("div", {});
      clear(resultsHost);
      resultsHost.appendChild(panel("Trace volume by service", host, { pad: true }));
      const series = (vol.series || []).map((s, i) => ({ label: s.label, color: colorFor(i), values: s.values.map(([t, v]) => [t, v]) }));
      mount(host, () => timeSeries(host, series, { height: 260, unit: "short", start: r.startMs, end: r.endMs, stacked: true, area: true }));
    } catch (err) {
      clear(resultsHost);
      resultsHost.appendChild(empty("Volume unavailable", err.message));
    }
  }

  function switchTab(id) {
    tab = id;
    clear(tabHost);
    tabHost.appendChild(tabs([{ id: "search", label: "Search" }, { id: "aggregates", label: "Aggregates & map" }, { id: "volume", label: "Volume" }], tab, switchTab));
    clear(scatterHost);
    if (tab === "search") search();
    if (tab === "aggregates") drawAggregates();
    if (tab === "volume") drawVolume();
  }

  // populate services
  try {
    const names = await ctx.api.labelValues("service");
    (names.data || []).forEach((s) => serviceSelect.appendChild(h("option", { value: s }, s)));
  } catch { /* optional */ }

  serviceSelect.addEventListener("change", search);
  statusSelect.addEventListener("change", search);
  sortSelect.addEventListener("change", search);
  opInput.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  durInput.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Traces"), h("p", { class: "sub" }, "Distributed traces over OTLP. Search by service, status or latency, then walk the waterfall.")),
    h("a", { class: "btn", href: "#/collector" }, "Send traces")));

  root.appendChild(h("div", { class: "panel panel-pad", style: { marginBottom: "12px" } },
    h("div", { class: "trace-search" }, serviceSelect, statusSelect, opInput, durInput, sortSelect)));

  root.appendChild(summaryHost);
  root.appendChild(tabHost);
  root.appendChild(scatterHost);
  root.appendChild(resultsHost);
  switchTab("search");
}

async function renderTrace(root, ctx, traceId) {
  const trace = await ctx.api.trace(traceId);
  ctx.setTitle(trace.root?.name || "Trace");
  const rootSpan = trace.spans.find((s) => !s.parent_id);
  const total = Math.max(...trace.spans.map((s) => s.start_ms + s.duration_ms));
  const colors = {};
  trace.services.forEach((s, i) => (colors[s] = colorFor(i)));

  const byParent = new Map();
  for (const s of trace.spans) {
    const key = s.parent_id || "root";
    if (!byParent.has(key)) byParent.set(key, []);
    byParent.get(key).push(s);
  }
  const depthOf = (span, depth = 0) => {
    const kids = byParent.get(span.span_id) || [];
    return [depth, ...kids.flatMap((k) => [depthOf(k, depth + 1)])];
  };

  const rows = [];
  const walk = (span, depth) => {
    rows.push({ span, depth });
    for (const child of byParent.get(span.span_id) || []) walk(child, depth + 1);
  };
  if (rootSpan) walk(rootSpan, 0); else trace.spans.forEach((s) => rows.push({ span: s, depth: 0 }));

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("button", { class: "btn ghost sm", onClick: () => ctx.go("/traces") }, "←"),
        h("h1", {}, trace.root?.name || "Trace"),
        trace.error ? badge("error", "crit") : badge("ok", "ok")),
      h("p", { class: "sub mono" }, trace.trace_id)),
    h("button", { class: "btn", onClick: () => { navigator.clipboard?.writeText(trace.trace_id); toast("Trace ID copied"); } }, "Copy ID")));

  root.appendChild(h("div", { class: "trace-summary" },
    h("div", {}, "Duration", h("strong", {}, byUnit(trace.duration_ms, "ms"))),
    h("div", {}, "Spans", h("strong", {}, trace.spans.length)),
    h("div", {}, "Services", h("strong", {}, trace.services.length)),
    h("div", {}, "Started", h("strong", {}, clock(trace.start_us / 1e6, true))),
    h("div", {}, "Status", h("strong", { style: { color: trace.error ? "var(--red)" : "var(--green)" } }, trace.error ? "error" : "ok"))));

  const waterfall = h("div", { class: "waterfall" });
  for (const { span, depth } of rows) {
    const left = (span.start_ms / total) * 100;
    const width = Math.max(0.4, (span.duration_ms / total) * 100);
    const isDb = (span.attrs?.["db.system"] || "").length > 0;
    const cls = span.status === "error" ? "err" : isDb ? "db" : span.kind === "client" ? "client" : "";
    const row = h("div", { class: "wf-row", style: { paddingLeft: `${depth * 12}px` } },
      h("div", { class: "wf-name" }, h("span", {}, span.name), h("span", { class: "svc" }, span.service)),
      h("div", { class: "wf-track" }, h("div", { class: `wf-bar ${cls}`, style: { left: `${left}%`, width: `${width}%`, background: cls === "" ? colors[span.service] || "#3f6d8f" : undefined } })),
      h("div", { class: "wf-dur" }, byUnit(span.duration_ms, "ms")));
    const detail = h("div", { class: "log-detail", style: { display: "none" } },
      h("pre", {}, JSON.stringify({ ...span.attrs, events: span.events }, null, 2)));
    row.addEventListener("click", () => { detail.style.display = detail.style.display === "none" ? "block" : "none"; });
    waterfall.appendChild(row);
    waterfall.appendChild(detail);
  }

  root.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap" } },
    h("div", { style: { flex: "2 1 560px", minWidth: "320px" } }, panel("Waterfall", waterfall, { pad: true })),
    h("div", { style: { flex: "1 1 260px", minWidth: "260px" } },
      panel("Services", h("div", {}, trace.services.map((s) => {
        const spans = trace.spans.filter((x) => x.service === s);
        const errs = spans.filter((x) => x.status === "error").length;
        return h("div", { class: "oncall" },
          h("span", { class: "dot", style: { background: colors[s] } }),
          h("div", { class: "oc-body" }, s, h("small", {}, `${spans.length} spans · ${byUnit(Math.max(...spans.map((x) => x.duration_ms)), "ms")}`)),
          errs ? h("span", { class: "badge crit" }, `${errs} err`) : h("span", { class: "badge ok" }, "ok"));
      })), { pad: true }),
      h("div", { class: "mt14" }, panel("Related", h("div", { class: "stack" },
        h("a", { class: "link", href: `#/logs?query=${encodeURIComponent(`{service=~".+"} |= "${trace.root?.service || ""}"`)}` }, "Logs for this service →"),
        h("a", { class: "link", href: "#/errors" }, "Errors correlated by trace →")), { pad: true })))));
}
