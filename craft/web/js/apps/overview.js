import { h, kpi, panel, badge, stateBadge, ago, num, byUnit, dur, empty, initials, colorFor } from "../ui.js";
import { timeSeries, mount, sparkline, volumeBars } from "../charts.js";

export async function render(root, ctx) {
  const r = ctx.range;
  const [ov, traffic, errors, latency, volume] = await Promise.all([
    ctx.api.overview(),
    ctx.api.queryRange("sum(rate(http_requests_total[5m]))", r.start, r.end, r.step),
    ctx.api.queryRange('sum(rate(http_requests_total{status=~"5.."}[5m]))', r.start, r.end, r.step),
    ctx.api.queryRange('histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))', r.start, r.end, r.step),
    ctx.api.queryRange('sum(rate(http_requests_total[5m])) by (service)', r.start, r.end, r.step),
  ]);
  ctx.setTitle("Overview");
  const hl = ov.headline;

  const wrap = h("div", {});

  // ---- incident banner
  if (ov.incidents.length) {
    const inc = ov.incidents[0];
    wrap.appendChild(
      h("a", { class: "banner", href: `#/incidents/${inc.id}`, style: { textDecoration: "none", color: "inherit" } },
        h("span", { class: "dot crit" }),
        h("div", { class: "grow" },
          h("h3", {}, `${inc.reference} · ${inc.title}`),
          h("p", {}, `${inc.severity} · ${inc.status} · ${dur(inc.duration_s)} · IC ${inc.commander || "unassigned"}`)),
        h("span", { class: "badge crit" }, "Active incident"),
        h("span", { class: "mono muted" }, "›"))
    );
  } else if (hl.firing) {
    wrap.appendChild(
      h("a", { class: "banner", href: "#/alerts", style: { textDecoration: "none", color: "inherit" } },
        h("span", { class: "dot warn" }),
        h("div", { class: "grow" }, h("h3", {}, `${hl.firing} alert${hl.firing === 1 ? "" : "s"} firing`), h("p", {}, "No incident declared yet — review the firing alerts.")),
        h("span", { class: "badge warn" }, "Action needed"),
        h("span", { class: "mono muted" }, "›"))
    );
  }

  // ---- headline KPIs
  wrap.appendChild(
    h("div", { class: "kpi-grid mb14" },
      kpi("Request rate", num(hl.rps), "requests / second", { tone: "accent" }),
      kpi("Error rate", hl.error_rate.toFixed(2) + "%", `${hl.firing} firing alerts`, { tone: hl.error_rate > 2 ? "bad" : hl.error_rate > 0.5 ? "warn" : "" }),
      kpi("p99 latency", byUnit(hl.p99_ms / 1000, "s"), `p50 ${byUnit(hl.p50_ms / 1000, "s")}`),
      kpi("Availability 24h", hl.uptime_24h.toFixed(3) + "%", `${hl.services} services`, { tone: hl.uptime_24h < 99.9 ? "warn" : "" }),
      kpi("Open incidents", hl.incidents, hl.incidents ? "responders engaged" : "all clear", { tone: hl.incidents ? "bad" : "", onClick: () => ctx.go("/incidents") }),
      kpi("Unresolved errors", hl.unresolved_issues, "error groups", { onClick: () => ctx.go("/errors") })
    )
  );

  // ---- main + side
  const trafficEl = h("div", {});
  const latencyEl = h("div", {});
  const volumeEl = h("div", {});

  const main = h("div", { class: "ov-main" },
    panel("Request volume & errors", trafficEl, {
      meta: ctx.range.long,
      pad: true,
      actions: h("a", { class: "btn sm ghost", href: "#/metrics" }, "Explore →"),
    }),
    panel("Latency (p95)", latencyEl, { meta: "5m rate" }),
    panel("Throughput by service", volumeEl, { pad: true })
  );

  const alertsPanel = panel("Active alerts", h("div", {}, ov.alerts.length
    ? ov.alerts.map((a) => h("div", { class: "alert-row", style: { cursor: "pointer" }, onClick: () => ctx.go("/alerts") },
        h("span", { class: `dot ${a.state === "firing" ? "crit" : "warn"}`, style: { marginTop: "5px" } }),
        h("div", { class: "a-body" },
          h("div", { class: "a-title" }, a.name),
          h("div", { class: "a-meta" }, `${a.severity} · ${Object.entries(a.labels).map(([k, v]) => `${k}=${v}`).join(" ") || "global"}`)),
        h("div", { class: "num muted", style: { fontSize: "10.5px" } }, ago(a.active_since))))
    : empty("Nothing firing", "All rules are within threshold.")), {
    actions: h("span", { class: "badge " + (hl.firing ? "crit" : "ok") }, hl.firing ? `${hl.firing} firing` : "clear"),
  });

  const sloPanel = panel("Error budgets", h("div", {}, ov.slos.length
    ? ov.slos.map((s) => {
        const remaining = s.budget_remaining_pct ?? 100;
        const tone = remaining < 10 ? "crit" : remaining < 40 ? "warn" : "";
        return h("div", { class: "budget" },
          h("div", { class: "top" },
            h("strong", {}, s.name),
            h("span", { class: `badge ${s.status === "breached" ? "crit" : "ok"}` }, s.objective * 100 + "%"),
            h("span", { class: "muted" }, (s.availability !== null && s.availability !== undefined ? (s.availability * 100).toFixed(3) + "%" : "—"))),
          h("div", { class: "bar" }, h("i", { class: tone, style: { width: Math.max(2, Math.min(100, remaining)) + "%" } })),
          h("div", { class: "hint", style: { marginTop: "4px" } }, `${remaining.toFixed(1)}% budget left · ${s.window_days}d window`));
      })
    : empty("No SLOs", "Define an objective to track error budgets.")), {
    actions: h("a", { class: "btn sm ghost", href: "#/slo" }, "Manage →"),
  });

  const oncallPanel = panel("Who's on call", h("div", {}, ov.on_call.map((o) =>
    h("div", { class: "oncall" },
      h("div", { class: "avatar" }, initials(o.on_call)),
      h("div", { class: "oc-body" }, o.on_call, h("small", {}, `${o.schedule} · ${o.rotation.replace(/_/g, " ")}`)),
      h("div", { class: "hint", style: { textAlign: "right" } }, "handoff", h("div", { class: "mono", style: { fontSize: "10.5px" } }, ago(o.next_handoff).replace(" ago", ""))))
  )));

  const statusPanel = panel("Status page", h("div", {}, [
    h("div", { class: "row", style: { alignItems: "center", marginBottom: "10px" } },
      h("span", { class: `dot ${ov.status_page.status === "operational" ? "ok" : "warn"}` }),
      h("strong", { style: { fontSize: "12.5px" } }, ov.status_page.status.replace(/_/g, " ")),
      h("div", { class: "spacer" }),
      h("a", { class: "btn sm ghost", href: "#/synthetics" }, "Open →")),
    ...ov.status_page.components.map((c) =>
      h("div", { class: "oncall" },
        h("span", { class: `dot ${c.ok ? "ok" : "warn"}` }),
        h("div", { class: "oc-body" }, c.group, h("small", {}, `${c.checks.length} checks`)),
        h("span", { class: "mono", style: { fontSize: "11px" } }, c.uptime_pct.toFixed(2) + "%"))),
  ]));

  const side = h("div", { class: "ov-side" }, alertsPanel, sloPanel, oncallPanel, statusPanel);
  wrap.appendChild(h("div", { class: "ov-grid" }, main, side));
  root.appendChild(wrap);

  // ---- mount charts
  const toSeries = (res, label, color) => ((res?.data?.result) || res?.result || []).map((s, i) => ({
    label: Object.keys(s.metric).length ? Object.entries(s.metric).map(([k, v]) => v).join(" ") : label,
    color: color || colorFor(i),
    values: s.values,
  }));

  mount(trafficEl, () => timeSeries(trafficEl, [
    ...toSeries(traffic, "requests", "#62a8ff"),
    ...toSeries(errors, "5xx", "#ff6b6b"),
  ], { height: 190, unit: "reqps", start: r.startMs, end: r.endMs, area: true }));

  mount(latencyEl, () => timeSeries(latencyEl, toSeries(latency, "p95", "#9ae66e"), { height: 150, unit: "s", start: r.startMs, end: r.endMs, area: true, legend: false }));

  mount(volumeEl, () => timeSeries(volumeEl, toSeries(volume), { height: 170, unit: "reqps", start: r.startMs, end: r.endMs, stacked: true, area: true }));

  // ---- service table
  const rows = ov.services.map((s) => {
    const errTone = s.error_rate > 2 ? "crit" : s.error_rate > 0.5 ? "warn" : "ok";
    return h("tr", { style: { cursor: "pointer" }, onClick: () => ctx.go(`/dashboards/service-overview?var-service=${s.name}`) },
      h("td", {}, h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("span", { class: `dot ${errTone}` }),
        h("div", {}, h("div", { style: { fontWeight: 550 } }, s.display), h("div", { class: "hint mono" }, s.name)))),
      h("td", {}, h("span", { class: "badge muted" }, s.team || "—")),
      h("td", { class: "num" }, s.tier),
      h("td", { class: "num" }, num(s.rps) + "/s"),
      h("td", { class: "num" }, h("span", { class: `badge ${errTone}` }, s.error_rate.toFixed(2) + "%")),
      h("td", { style: { width: "120px" } }, h("div", { class: "bar", style: { height: "4px", background: "#1e242c", borderRadius: "3px", overflow: "hidden" } },
        h("i", { style: { display: "block", height: "100%", width: Math.min(100, s.error_rate * 12) + "%", background: errTone === "crit" ? "#ff6b6b" : errTone === "warn" ? "#f5c451" : "#6fd68a" } })))
    );
  });

  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Services"), h("div", { class: "grow" }), h("span", { class: "hint" }, "RED signals · last 5 minutes")),
    panel("", h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["Service", "Team", "Tier", "Rate", "Errors", "Error share"].map((c) => h("th", { class: c === "Tier" || c === "Rate" || c === "Errors" ? "right" : "" }, c)))),
      h("tbody", {}, rows))), { pad: false })));

  // sparkline strips of per-service errors (uses existing volume data)
  return () => {};
}
