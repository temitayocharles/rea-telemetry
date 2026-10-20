import { h, clear, empty, toast, modal, formField, byUnit } from "../ui.js";
import { timeSeries, mount, gauge, heatmap, barList } from "../charts.js";

export async function render(root, ctx) {
  const { parts, params } = ctx.route;
  if (parts[1]) return renderDashboard(root, ctx, parts[1], params);
  return renderList(root, ctx);
}

async function renderList(root, ctx) {
  ctx.setTitle("Dashboards");
  const { dashboards } = await ctx.api.dashboards();
  const folders = [...new Set(dashboards.map((d) => d.folder))];

  const newBtn = h("button", { class: "btn primary", onClick: () => openNewDashboard(ctx) }, "New dashboard");

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Dashboards"), h("p", { class: "sub" }, "Panels, variables and layouts — the same model Grafana uses, stored locally.")),
    newBtn));

  for (const folder of folders) {
    const items = dashboards.filter((d) => d.folder === folder);
    root.appendChild(h("div", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, folder), h("span", { class: "hint" }, `${items.length} dashboard${items.length === 1 ? "" : "s"}`)),
      h("div", { class: "dash-cards" }, items.map((d) =>
        h("div", { class: "panel dash-card", onClick: () => ctx.go(`/dashboards/${d.uid}`) },
          h("div", { class: "row", style: { alignItems: "flex-start" } },
            h("div", { class: "flex1" }, h("h3", {}, d.title), h("div", { class: "meta" }, `${d.panel_count} panels · updated ${new Date(d.updated_at * 1000).toLocaleDateString()}`)),
            h("button", { class: "btn ghost sm", title: "Star", onClick: async (e) => { e.stopPropagation(); await ctx.api.starDashboard(d.uid); ctx.refresh(); } }, d.starred ? "★" : "☆")),
          h("div", { class: "thumb" }, h("div", { style: { position: "absolute", inset: "8px", display: "flex", gap: "4px" } },
            Array.from({ length: Math.min(6, d.panel_count) }).map((_, i) =>
              h("div", { style: { flex: 1, borderRadius: "3px", background: `linear-gradient(180deg, rgba(154,230,110,${0.16 - i * 0.02}), transparent)`, border: "1px solid #1e242c" } })))),
          h("div", { class: "tags" }, d.tags.map((t) => h("span", { class: "chip" }, t))))))));
  }
}

function openNewDashboard(ctx) {
  const titleInput = h("input", { class: "input", placeholder: "Payments overview" });
  const folderInput = h("input", { class: "input", placeholder: "General", value: "General" });
  const m = modal({
    title: "New dashboard",
    body: [formField("Title", titleInput), formField("Folder", folderInput)],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        const title = titleInput.value.trim();
        if (!title) return toast("A title is required", "err");
        const uid = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") + "-" + Math.random().toString(36).slice(2, 5);
        await ctx.api.saveDashboard({ uid, title, folder: folderInput.value || "General", panels: [], templating: [], tags: ["new"] });
        m.close();
        ctx.go(`/dashboards/${uid}?edit=1`);
      } }, "Create"),
    ],
  });
}

function substitute(expr, values) {
  return expr.replace(/\$(\w+)|\[\$(\w+)\]/g, (_, a, b) => {
    const name = a || b;
    const v = values[name];
    if (v === undefined) return ".*";
    return v;
  });
}

async function renderDashboard(root, ctx, uid, params) {
  let dash;
  try { dash = await ctx.api.dashboard(uid); }
  catch { root.appendChild(empty("Dashboard not found", uid, h("button", { class: "btn", onClick: () => ctx.go("/dashboards") }, "Back to dashboards"))); return; }
  ctx.setTitle(dash.title);
  let edit = params.get("edit") === "1";
  const r = ctx.range;
  const varValues = {};
  for (const v of dash.templating || []) {
    const fromUrl = params.get(`var-${v.name}`);
    varValues[v.name] = fromUrl || (v.includeAll ? ".*" : "");
  }
  const charts = [];

  const varBar = h("div", { class: "var-bar" });
  const grid = h("div", { class: "dash-grid" });
  const head = h("div", { class: "page-head" });
  root.appendChild(head);
  if ((dash.templating || []).length) root.appendChild(varBar);
  root.appendChild(grid);

  function drawHead() {
    clear(head);
    head.appendChild(h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("button", { class: "btn ghost sm", onClick: () => ctx.go("/dashboards") }, "←"),
        h("h1", {}, dash.title),
        edit ? h("span", { class: "badge warn" }, "editing") : null),
      h("p", { class: "sub" }, `${dash.folder} · ${(dash.tags || []).join(", ") || "no tags"} · ${dash.panels.length} panels`)));
    head.appendChild(h("div", { class: "row", style: { gap: "6px" } },
      edit ? h("button", { class: "btn primary", onClick: saveAll }, "Save") : null,
      h("button", { class: "btn", onClick: () => openAddPanel() }, edit ? "Add panel" : "Add panel"),
      h("button", { class: "btn", onClick: () => { edit = !edit; drawHead(); drawPanels(); } }, edit ? "Exit edit" : "Edit"),
      h("a", { class: "btn ghost", href: `/dashboards/${uid}?edit=1` }, "Share"),
      h("button", { class: "btn ghost", onClick: () => exportDashboard() }, "JSON")));
  }

  async function drawVars() {
    clear(varBar);
    if (!(dash.templating || []).length) return;
    for (const v of dash.templating) {
      let values = [];
      try {
        const q = v.query || "";
        const m = q.match(/label_values\((\w+)\)/);
        if (m) {
          const res = await ctx.api.labelValues(m[1]);
          values = res.data || [];
        }
      } catch { /* keep empty */ }
      const current = varValues[v.name];
      const select = h("select", { class: "select", onChange: (e) => {
        varValues[v.name] = e.target.value;
        const url = new URL(location.href);
        url.searchParams.set(`var-${v.name}`, e.target.value);
        history.replaceState(null, "", url);
        drawPanels();
      } },
        v.includeAll ? h("option", { value: ".*", selected: current === ".*" }, "All") : null,
        ...values.map((val) => h("option", { value: val, selected: current === val }, val)));
      varBar.appendChild(h("div", { class: "row", style: { alignItems: "center" } }, h("label", {}, v.name), select));
    }
    varBar.appendChild(h("div", { class: "spacer" }));
    varBar.appendChild(h("span", { class: "hint" }, `${ctx.range.long} · refreshes${ctx.state ? " automatically" : ""}`));
  }

  async function drawPanels() {
    for (const c of charts) c?.();
    charts.length = 0;
    clear(grid);
    if (!dash.panels.length) {
      grid.appendChild(h("div", { style: { gridColumn: "1 / span 24" } }, empty("This dashboard is empty", "Add a panel to start visualising.", h("button", { class: "btn primary", onClick: () => openAddPanel() }, "Add panel"))));
      return;
    }
    for (const p of dash.panels) {
      const el = h("div", { class: "dash-panel", style: { gridColumn: `${(p.gridPos?.x || 0) + 1} / span ${p.gridPos?.w || 12}`, gridRow: `span ${p.gridPos?.h || 6}` } });
      el.appendChild(h("div", { class: "dp-head" },
        h("span", {}, p.title),
        h("span", { class: "muted" }, p.unit !== "short" ? p.unit : ""),
        edit ? h("div", { class: "row", style: { marginLeft: "auto", gap: "3px" } },
          h("button", { class: "btn sm ghost", onClick: () => openPanelEditor(p) }, "edit"),
          h("button", { class: "btn sm ghost", onClick: () => { dash.panels = dash.panels.filter((x) => x !== p); drawPanels(); } }, "×")) : null));
      const body = h("div", { class: "dp-body" });
      el.appendChild(body);
      grid.appendChild(el);
      renderPanel(body, p, charts);
    }
  }

  function renderPanel(body, p, cleanups) {
    const targets = (p.targets || []).filter((t) => t.expr);
    if (!targets.length) { body.appendChild(h("div", { class: "empty" }, "No query")); return; }
    const unit = p.unit || "short";
    if (["timeseries", "heatmap", "logs", "table", "bargauge"].includes(p.type)) {
      Promise.all(targets.map((t) => ctx.api.queryRange(substitute(t.expr, varValues), r.start, r.end, r.step).catch(() => ({ data: { result: [] } })))).then((results) => {
        const series = [];
        results.forEach((res, ti) => {
          for (const s of res.data.result || []) {
            series.push({ label: legendFor(s.metric, targets[ti].legend || "{{service}}"), values: s.values });
          }
        });
        if (p.type === "timeseries") {
          cleanups.push(mount(body, () => timeSeries(body, series, { height: Math.max(90, (p.gridPos?.h || 6) * 30 - 42), unit, start: r.startMs, end: r.endMs, area: (p.options || {}).area, stacked: (p.options || {}).stacked })));
        } else if (p.type === "heatmap") {
          const buckets = [];
          const byLe = {};
          for (const res of results) for (const s of res.data.result || []) {
            const le = parseFloat(s.metric.le);
            if (Number.isNaN(le)) continue;
            (byLe[le] = byLe[le] || { le, values: s.values });
          }
          Object.values(byLe).forEach((b) => buckets.push(b));
          cleanups.push(mount(body, () => heatmap(body, buckets, { height: Math.max(90, (p.gridPos?.h || 6) * 30 - 42) })));
        } else if (p.type === "logs") {
          body.appendChild(h("div", { class: "empty" }, "Log panels live in Logs — open the explorer for live rows."));
        } else if (p.type === "bargauge") {
          const rows = series.map((s) => ({ label: s.label, value: parseFloat(s.values[s.values.length - 1]?.[1] || 0) }));
          cleanups.push(mount(body, () => barList(body, rows.sort((a, b) => b.value - a.value).slice(0, 10), { format: (v) => byUnit(v, unit) })));
        } else {
          const cols = [{ label: "Series", render: (row) => row.label }, { label: "Last", align: "right", render: (row) => byUnit(row.last, unit) }, { label: "Max", align: "right", render: (row) => byUnit(row.max, unit) }];
          const rows = series.map((s) => {
            const vals = s.values.map((v) => parseFloat(v[1])).filter((v) => !Number.isNaN(v));
            return { label: s.label, last: vals[vals.length - 1], max: Math.max(...vals) };
          }).sort((a, b) => (b.last || 0) - (a.last || 0));
          body.appendChild(h("div", { style: { maxHeight: "100%", overflow: "auto" } }, h("table", { class: "data" },
            h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.align }, c.label)))),
            h("tbody", {}, rows.map((row) => h("tr", {}, cols.map((c) => h("td", { class: c.align === "right" ? "num" : "" }, c.render(row)))))))));
        }
      });
      return;
    }
    // instant panels
    Promise.all(targets.map((t) => ctx.api.query(substitute(t.expr, varValues)).catch(() => ({ data: { result: [] } })))).then((results) => {
      const flat = results.flatMap((res) => res.data.result || []);
      if (!flat.length) { body.appendChild(h("div", { class: "empty" }, "No data")); return; }
      const numeric = flat.map((s) => parseFloat(Array.isArray(s.value) ? s.value[1] : s.values?.[s.values.length - 1]?.[1]));
      if (p.type === "stat") {
        const first = numeric[0];
        const tone = unit === "percentunit" && first < 0.999 ? "#f5c451" : "#e9ecef";
        body.appendChild(h("div", { class: "panel-stat", style: { color: tone } }, byUnit(first, unit), h("small", {}, flat.length > 1 ? `${flat.length} series` : "")));
      } else if (p.type === "gauge") {
        body.appendChild(gauge(numeric[0], { size: 150, label: byUnit(numeric[0], unit), sub: p.title }));
      } else if (p.type === "piechart") {
        const rows = flat.map((s, i) => ({ label: legendFor(s.metric, "value"), value: parseFloat(s.value?.[1] ?? 0) }));
        cleanups.push(mount(body, () => barList(body, rows.slice(0, 8), { format: (v) => byUnit(v, unit) })));
      } else {
        body.appendChild(h("div", { class: "panel-stat" }, byUnit(numeric[0], unit)));
      }
    });
  }

  function legendFor(metric, template) {
    let out = template || "{{service}}";
    out = out.replace(/\{\{(\w+)\}\}/g, (_, k) => metric[k] ?? "");
    return out || Object.entries(metric).filter(([k]) => k !== "__name__").map(([, v]) => v).join(" ") || metric.__name__ || "value";
  }

  function openAddPanel() {
    openPanelEditor(null);
  }

  function openPanelEditor(existing) {
    const panelObj = existing || { id: Date.now(), title: "New panel", type: "timeseries", unit: "short", gridPos: { x: 0, y: 0, w: 12, h: 6 }, targets: [{ refId: "A", expr: "sum(rate(http_requests_total[5m])) by (service)", legend: "{{service}}" }] };
    const titleIn = h("input", { class: "input", value: panelObj.title });
    const exprIn = h("textarea", { class: "textarea", value: panelObj.targets[0]?.expr || "" });
    const legendIn = h("input", { class: "input mono", value: panelObj.targets[0]?.legend || "{{service}}" });
    const typeIn = h("select", { class: "select" }, ["timeseries", "stat", "gauge", "table", "bargauge", "heatmap", "piechart", "logs"].map((t) => h("option", { value: t, selected: panelObj.type === t }, t)));
    const unitIn = h("select", { class: "select" }, ["short", "percentunit", "percent", "s", "ms", "bytes", "reqps", "currencyUSD"].map((u) => h("option", { value: u, selected: panelObj.unit === u }, u)));
    const wIn = h("input", { class: "input", type: "number", value: panelObj.gridPos?.w || 12, min: 3, max: 24 });
    const hIn = h("input", { class: "input", type: "number", value: panelObj.gridPos?.h || 6, min: 2, max: 16 });
    const preview = h("div", {});
    const runPreview = async () => {
      clear(preview);
      try {
        const res = await ctx.api.queryRange(exprIn.value, r.start, r.end, r.step);
        const series = (res.data.result || []).map((s) => ({ label: legendFor(s.metric, legendIn.value), values: s.values }));
        const el = h("div", {});
        preview.appendChild(el);
        mount(el, () => timeSeries(el, series, { height: 140, unit: unitIn.value, start: r.startMs, end: r.endMs }));
      } catch (err) { preview.appendChild(h("div", { class: "hint" }, err.message)); }
    };
    setTimeout(runPreview, 50);
    const m = modal({
      title: existing ? "Edit panel" : "Add panel",
      wide: true,
      body: [
        h("div", { class: "form-grid" }, formField("Title", titleIn), formField("Visualisation", typeIn), formField("Unit", unitIn)),
        formField("Query (PromQL)", exprIn, "Use $variables to link dashboard variables."),
        h("div", { class: "form-grid" }, formField("Legend", legendIn), formField("Width (1-24)", wIn), formField("Height (rows)", hIn)),
        h("div", { class: "row" }, h("button", { class: "btn sm", onClick: runPreview }, "Preview"), h("span", { class: "hint" }, "Preview renders with the current time range")),
        preview,
      ],
      footer: [
        existing ? h("button", { class: "btn danger", onClick: () => { dash.panels = dash.panels.filter((p) => p !== existing); m.close(); drawPanels(); } }, "Delete") : null,
        h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
        h("button", { class: "btn primary", onClick: () => {
          const updated = {
            ...panelObj,
            title: titleIn.value || "Panel",
            type: typeIn.value,
            unit: unitIn.value,
            targets: [{ refId: "A", expr: exprIn.value, legend: legendIn.value }],
            gridPos: { ...panelObj.gridPos, w: Number(wIn.value), h: Number(hIn.value) },
          };
          if (existing) Object.assign(existing, updated);
          else dash.panels.push(updated);
          m.close();
          drawPanels();
        } }, "Apply"),
      ],
    });
  }

  async function saveAll() {
    await ctx.api.saveDashboard({ uid: dash.uid, title: dash.title, folder: dash.folder, tags: dash.tags, panels: dash.panels, templating: dash.templating });
    toast("Dashboard saved");
  }

  function exportDashboard() {
    const blob = new Blob([JSON.stringify(dash, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${dash.uid}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  drawHead();
  await drawVars();
  await drawPanels();
  ctx.setCleanup(() => charts.forEach((c) => c?.()));
}
