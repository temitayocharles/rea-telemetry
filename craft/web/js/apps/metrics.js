import { h, clear, panel, badge, debounce, empty, byUnit, num, tabs, toast } from "../ui.js";
import { timeSeries, mount } from "../charts.js";

const PRESETS = [
  ["Request rate", "sum(rate(http_requests_total[5m])) by (service)"],
  ["Error ratio", 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (service) / sum(rate(http_requests_total[5m])) by (service)'],
  ["p99 latency", 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))'],
  ["CPU by service", "sum(app_cpu_usage_ratio) by (service)"],
  ["Memory by service", "sum(app_memory_working_set_bytes) by (service)"],
  ["Disk free", "node_filesystem_free_bytes"],
  ["Queue backlog", "sum(queue_messages_ready) by (queue)"],
  ["Scrape health", "up"],
];

export async function render(root, ctx) {
  ctx.setTitle("Metrics");
  const params = ctx.route.params;
  let query = params.get("query") || localStorage.getItem("craft.lastQuery") || "sum(rate(http_requests_total[5m])) by (service)";
  let mode = "range";
  let activeTab = "graph";
  let metricFilter = "";
  let catalog = [];
  let chartCleanup = null;

  const qInput = h("input", { class: "input mono q-input", value: query, spellcheck: "false" });
  const resultHost = h("div", {});
  const metaHost = h("div", { class: "row", style: { alignItems: "center", gap: "10px" } });
  const catalogHost = h("div", { class: "metric-list" });
  const filterInput = h("input", { class: "input", placeholder: "Filter metrics…", style: { margin: "8px" } });
  filterInput.addEventListener("input", debounce(() => { metricFilter = filterInput.value.toLowerCase(); drawCatalog(); }, 120));

  const runBtn = h("button", { class: "btn primary" }, "Run query");
  const instantBtn = h("button", { class: "btn" }, "Instant");
  const presetBar = h("div", { class: "chips mb8" }, PRESETS.map(([label, expr]) =>
    h("button", { class: "chip add", onClick: () => { qInput.value = expr; run(); } }, label)));

  async function run() {
    query = qInput.value.trim();
    if (!query) return;
    localStorage.setItem("craft.lastQuery", query);
    clear(resultHost);
    resultHost.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
    const t0 = performance.now();
    try {
      if (mode === "instant") {
        const res = await ctx.api.query(query);
        const elapsed = Math.round(performance.now() - t0);
        renderInstant(res.data, elapsed);
      } else {
        const r = ctx.range;
        const res = await ctx.api.queryRange(query, r.start, r.end, r.step);
        const elapsed = Math.round(performance.now() - t0);
        renderRange(res.data, elapsed);
      }
    } catch (err) {
      clear(resultHost);
      resultHost.appendChild(empty("Query failed", err.message));
      clear(metaHost);
    }
  }

  function renderRange(data, elapsed) {
    clear(resultHost); clear(metaHost);
    const count = (data.result || []).length;
    metaHost.appendChild(h("span", { class: "badge ok" }, `${count} series`));
    metaHost.appendChild(h("span", { class: "hint" }, `${elapsed}ms · ${ctx.range.long} · step ${ctx.range.step}s`));
    if (!count) { resultHost.appendChild(empty("No series", "Try loosening label matchers or widening the range.")); return; }
    const chartEl = h("div", {});
    resultHost.appendChild(h("div", { class: "panel", style: { marginBottom: "12px" } }, h("div", { class: "panel-body" }, chartEl)));
    chartCleanup?.stop?.(); // mount() returns { redraw, stop }
    const series = data.result.map((s, i) => ({
      label: labelOf(s.metric),
      values: s.values,
    }));
    chartCleanup = mount(chartEl, () => timeSeries(chartEl, series, {
      height: 300, start: ctx.range.startMs, end: ctx.range.endMs, unit: unitGuess(query),
    }));

    // result table with last value + summary
    const rows = data.result.map((s) => {
      const vals = s.values.map((v) => parseFloat(v[1])).filter((v) => !Number.isNaN(v));
      const last = vals[vals.length - 1];
      const min = Math.min(...vals), max = Math.max(...vals), avg = vals.reduce((a, b) => a + b, 0) / (vals.length || 1);
      return { labels: s.metric, last, min, max, avg };
    }).sort((a, b) => (b.last || 0) - (a.last || 0));
    resultHost.appendChild(panel("Result", h("div", { class: "result-table" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, [h("th", {}, "Series"), h("th", { class: "right" }, "Last"), h("th", { class: "right" }, "Min"), h("th", { class: "right" }, "Max"), h("th", { class: "right" }, "Avg")])),
      h("tbody", {}, rows.map((row) => h("tr", {},
        h("td", {}, h("div", { class: "row", style: { gap: "4px", flexWrap: "wrap" } },
          Object.entries(row.labels).filter(([k]) => k !== "__name__").map(([k, v]) => h("span", { class: "kv-chip" }, `${k}=${v}`)))),
        h("td", { class: "num" }, byUnit(row.last, unitGuess(query))),
        h("td", { class: "num" }, byUnit(row.min, unitGuess(query))),
        h("td", { class: "num" }, byUnit(row.max, unitGuess(query))),
        h("td", { class: "num" }, byUnit(row.avg, unitGuess(query)))
      ))))), { pad: false }));
  }

  function renderInstant(data, elapsed) {
    clear(resultHost); clear(metaHost);
    metaHost.appendChild(h("span", { class: "badge ok" }, data.resultType));
    metaHost.appendChild(h("span", { class: "hint" }, `${elapsed}ms · evaluated at ${new Date().toLocaleTimeString()}`));
    if (data.resultType === "scalar") {
      resultHost.appendChild(h("div", { class: "kpi accent", style: { maxWidth: "260px" } }, h("div", { class: "k-label" }, "Scalar"), h("div", { class: "k-value" }, data.result[1])));
      return;
    }
    const rows = data.resultType === "matrix"
      ? data.result.map((s) => ({ labels: s.metric, value: s.values.length, ts: s.values[s.values.length - 1]?.[0] }))
      : data.result.map((s) => ({ labels: s.metric, value: parseFloat(s.value[1]), ts: s.value[0] }));
    if (!rows.length) { resultHost.appendChild(empty("Empty vector", "The expression matched no series at this instant.")); return; }
    resultHost.appendChild(panel("", h("table", { class: "data" },
      h("thead", {}, h("tr", {}, [h("th", {}, "Series"), h("th", { class: "right" }, "Value")])),
      h("tbody", {}, rows.map((row) => h("tr", {},
        h("td", {}, h("div", { class: "row", style: { gap: "4px", flexWrap: "wrap" } },
          Object.entries(row.labels).filter(([k]) => k !== "__name__").map(([k, v]) => h("span", { class: "kv-chip" }, `${k}=${v}`)))),
        h("td", { class: "num" }, byUnit(row.value, unitGuess(query))))))), { pad: false }));
  }

  const labelOf = (metric) => {
    const entries = Object.entries(metric).filter(([k]) => k !== "__name__");
    if (!entries.length) return metric.__name__ || "value";
    return entries.map(([k, v]) => (k === "le" ? `≤${v}` : v)).join(" · ");
  };

  const unitGuess = (q) => {
    if (/_seconds_bucket|_seconds|latency|duration/.test(q)) return "s";
    if (/bytes|memory|filesystem/.test(q)) return "bytes";
    if (/cpu_usage_ratio|histogram_quantile/.test(q)) return q.includes("cpu") ? "percentunit" : "s";
    if (/requests_total|_total/.test(q) && /rate\(/.test(q)) return "reqps";
    return "short";
  };

  function drawCatalog() {
    clear(catalogHost);
    const list = catalog.filter((m) => !metricFilter || m.name.toLowerCase().includes(metricFilter));
    catalogHost.appendChild(h("div", { class: "hint", style: { padding: "4px 10px" } }, `${list.length} metrics`));
    for (const m of list) {
      catalogHost.appendChild(h("div", {
        class: "metric-item",
        onClick: () => { qInput.value = m.name; run(); },
        title: `${m.name} — ${m.series} series, ${m.samples.toLocaleString()} samples`,
      },
        h("div", { class: "m-name" }, m.name),
        h("div", { class: "m-meta" }, h("span", {}, `${m.series} series`), h("span", {}, m.type))));
    }
  }

  qInput.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  runBtn.addEventListener("click", run);
  instantBtn.addEventListener("click", () => {
    mode = mode === "range" ? "instant" : "range";
    instantBtn.textContent = mode === "instant" ? "Back to range" : "Instant";
    run();
  });

  // tabs: graph / labels / targets
  const tabHost = h("div", {});
  const bodyHost = h("div", {});
  function switchTab(id) {
    activeTab = id;
    clear(tabHost);
    tabHost.appendChild(tabs([{ id: "graph", label: "Query" }, { id: "labels", label: "Labels" }, { id: "targets", label: "Scrape targets" }], activeTab, switchTab));
    clear(bodyHost);
    if (id === "graph") bodyHost.appendChild(graphPane);
    if (id === "labels") renderLabels();
    if (id === "targets") renderTargets();
  }

  const graphPane = h("div", {},
    h("div", { class: "panel panel-pad", style: { marginBottom: "12px" } },
      h("div", { class: "query-bar mb8" }, qInput, runBtn, instantBtn),
      presetBar,
      h("div", { class: "row", style: { alignItems: "center" } }, metaHost)),
    resultHost);

  async function renderLabels() {
    bodyHost.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
    try {
      const { data: names } = await ctx.api.labels();
      clear(bodyHost);
      const detail = h("div", {});
      const list = h("div", { class: "panel", style: { maxHeight: "540px", overflow: "auto" } },
        h("div", { class: "panel-head" }, h("h3", {}, "Label names")),
        names.map((n) => h("div", { class: "metric-item", onClick: async () => {
          clear(detail);
          detail.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
          const { data: values } = await ctx.api.labelValues(n);
          clear(detail);
          detail.appendChild(panel(`Values for ${n}`, h("div", { class: "chips" },
            values.slice(0, 400).map((v) => h("button", { class: "chip add", onClick: () => { qInput.value = `{${n}="${v}"}`; run(); } }, v))), { pad: true }));
        } }, h("div", { class: "m-name" }, n))));
      bodyHost.appendChild(h("div", { class: "explore-grid" }, list, detail));
    } catch (err) {
      clear(bodyHost);
      bodyHost.appendChild(empty("Could not load labels", err.message));
    }
  }

  async function renderTargets() {
    clear(bodyHost);
    try {
      const { targets } = await ctx.api.targets();
      const urlInput = h("input", { class: "input mono", placeholder: "http://localhost:9100/metrics" });
      const jobInput = h("input", { class: "input", placeholder: "job name", value: "custom" });
      const listHost = h("div", {});
      const draw = (items) => {
        clear(listHost);
        listHost.appendChild(h("table", { class: "data" },
          h("thead", {}, h("tr", {}, ["URL", "Job", "Instance", "Interval", ""].map((c) => h("th", {}, c)))),
          h("tbody", {}, items.map((t, i) => h("tr", {},
            h("td", { class: "mono" }, t.url),
            h("td", {}, t.job),
            h("td", { class: "mono" }, t.instance || "—"),
            h("td", { class: "num" }, (t.interval_s || 30) + "s"),
            h("td", { class: "right" }, h("div", { class: "row", style: { justifyContent: "flex-end", gap: "4px" } },
              h("button", { class: "btn sm", onClick: async () => {
                const res = await ctx.api.scrape(t);
                toast(res.ok ? `Scraped ${res.samples} samples in ${res.duration_ms}ms` : `Scrape failed: ${res.error}`, res.ok ? "" : "err");
              } }, "Scrape now"),
              h("button", { class: "btn sm danger", onClick: async () => {
                items.splice(i, 1); await ctx.api.saveTargets(items); draw(items); toast("Target removed");
              } }, "Remove"))))))));
      };
      draw(targets);
      bodyHost.appendChild(panel("Add a scrape target", h("div", {},
        h("div", { class: "form-grid" }, h("div", { class: "field" }, h("label", {}, "Metrics URL"), urlInput), h("div", { class: "field" }, h("label", {}, "Job"), jobInput)),
        h("div", { class: "mt14" }, h("button", { class: "btn primary", onClick: async () => {
          if (!urlInput.value) return toast("Enter a metrics URL", "err");
          const next = [...targets, { url: urlInput.value, job: jobInput.value, instance: urlInput.value, interval_s: 30, enabled: true }];
          await ctx.api.saveTargets(next);
          targets.push(next[next.length - 1]);
          draw(targets); urlInput.value = "";
          toast("Target added — it is scraped by the collector loop");
        } }, "Add target"))), { pad: true }));
      bodyHost.appendChild(h("div", { class: "mt14" }, panel("Targets", h("div", {}, listHost), { pad: false })));
    } catch (err) {
      bodyHost.appendChild(empty("Targets unavailable", err.message));
    }
  }

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Metrics"), h("p", { class: "sub" }, "PromQL over one local time-series store. Rate, histogram_quantile, aggregations and arithmetic all evaluate here.")),
    h("a", { class: "btn", href: "#/dashboards" }, "Dashboards →")));

  const sidebar = h("div", { class: "panel" },
    h("div", { class: "panel-head" }, h("h3", {}, "Metric catalog"), h("span", { class: "spacer" })),
    filterInput,
    catalogHost);
  root.appendChild(h("div", { class: "explore-grid" }, sidebar, h("div", {}, tabHost, bodyHost)));

  try {
    const res = await ctx.api.metrics();
    catalog = res.metrics || [];
    drawCatalog();
  } catch { catalog = []; drawCatalog(); }

  switchTab("graph");
  run();
  ctx.setCleanup(() => { chartCleanup?.stop?.(); });
}
