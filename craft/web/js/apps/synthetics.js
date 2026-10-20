import { h, clear, panel, badge, empty, ago, num, toast, modal, formField, confirmDialog, clock, dur } from "../ui.js";

export async function render(root, ctx) {
  ctx.setTitle("Synthetics");
  const [checksRes, status] = await Promise.all([ctx.api.checks(), ctx.api.statusPage()]);
  const checks = checksRes.checks;
  const down = checks.filter((c) => c.last && !c.last.ok).length;

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("h1", {}, "Synthetics"),
        down ? badge(`${down} failing`, "crit") : badge("all passing", "ok")),
      h("p", { class: "sub" }, "HTTP checks from multiple regions with uptime history, latency and a public status page.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("button", { class: "btn", onClick: async () => { await Promise.all(checks.map((c) => ctx.api.runCheck(c.id).catch(() => null))); toast("All checks executed"); ctx.refresh(); } }, "Run all now"),
      h("button", { class: "btn primary", onClick: () => openCheck(ctx, null) }, "New check"))));

  root.appendChild(h("div", { class: "kpi-grid mb14" },
    kpi("Checks", checks.length, "enabled"),
    kpi("Global uptime 24h", avgUptime(checks).toFixed(3) + "%", "across all regions", avgUptime(checks) < 99.9 ? "warn" : ""),
    kpi("Median latency", medianLatency(checks) + "ms", "last sample"),
    kpi("Failures 24h", checks.reduce((a, c) => a + (c.uptime_24h.failures || 0), 0), "failed probes", down ? "bad" : "")));

  const table = h("table", { class: "data checks-table" },
    h("thead", {}, h("tr", {}, ["Check", "Group", "URL", "Regions", "Uptime 24h", "Latency", "Last run", "History", ""].map((c, i) => h("th", { class: i >= 4 && i <= 6 ? "right" : "" }, c)))),
    h("tbody", {}, checks.map((c) => {
      const ok = c.last?.ok;
      return h("tr", {},
        h("td", {}, h("div", { class: "row", style: { alignItems: "center", gap: "7px" } },
          h("span", { class: `dot ${c.last ? (ok ? "ok" : "crit") : "muted"}` }),
          h("div", {}, h("div", { style: { fontWeight: 550 } }, c.name), h("div", { class: "hint mono" }, `${c.method} expect ${c.expect_status} · every ${c.interval_s}s`)))),
        h("td", {}, h("span", { class: "badge muted" }, c.group_name)),
        h("td", { class: "mono", style: { maxWidth: "260px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, c.url),
        h("td", {}, h("div", { class: "chips" }, (c.regions || []).map((r) => h("span", { class: "chip" }, r)))),
        h("td", { class: "num" }, h("span", { class: `badge ${c.uptime_24h.uptime_pct >= 99.5 ? "ok" : c.uptime_24h.uptime_pct >= 95 ? "warn" : "crit"}` }, c.uptime_24h.uptime_pct.toFixed(2) + "%")),
        h("td", { class: "num" }, c.last?.latency_ms ? c.last.latency_ms.toFixed(0) + "ms" : "—"),
        h("td", { class: "num muted" }, c.last ? ago(c.last.ts) : "never"),
        h("td", {}, h("div", { class: "bars" }, c.history.slice(-40).map((h2) => h("i", { class: h2.ok ? "" : "down", title: `${clock(h2.ts)} · ${h2.latency_ms}ms${h2.error ? " · " + h2.error : ""}` })))),
        h("td", { class: "right" }, h("div", { class: "row", style: { gap: "4px", justifyContent: "flex-end" } },
          h("button", { class: "btn sm", onClick: async () => { const r = await ctx.api.runCheck(c.id); toast(r.ok ? `${c.name}: ${r.status_code} in ${Math.round(r.latency_ms)}ms` : `${c.name}: ${r.error}`, r.ok ? "" : "err"); ctx.refresh(); } }, "Run"),
          h("button", { class: "btn sm ghost", onClick: () => openCheck(ctx, c) }, "Edit"),
          h("button", { class: "btn sm ghost", onClick: () => confirmDialog("Delete check?", c.name, async () => { await ctx.api.deleteCheck(c.id); toast("Check deleted"); ctx.refresh(); }, "Delete", true) }, "×"))));
    })));

  root.appendChild(panel("", h("div", { class: "table-wrap" }, table), { pad: false }));

  // ---------------- status page preview
  const statusHost = h("div", {});
  const bannerClass = status.status === "operational" ? "" : status.status === "degraded" ? "degraded" : "outage";
  statusHost.appendChild(h("div", { class: `status-hero ${bannerClass}` },
    h("span", { class: `dot ${status.status === "operational" ? "ok" : status.status === "degraded" ? "warn" : "crit"}`, style: { width: "11px", height: "11px" } }),
    h("div", { class: "grow" }, h("h2", {}, status.status === "operational" ? "All systems operational" : status.status === "degraded" ? "Some systems degraded" : "Partial outage"),
      h("p", {}, `Updated ${clock(status.generated_at, true)} · checks run from ${[...new Set(checks.flatMap((c) => c.regions || []))].join(", ")}`)),
    h("button", { class: "btn", onClick: () => window.open("/#/synthetics", "_blank") }, "Open page")));

  for (const comp of status.components) {
    const group = h("div", { class: "panel status-group", style: { marginBottom: "10px" } });
    group.appendChild(h("div", { class: "sg-head" },
      h("span", { class: `dot ${comp.ok ? "ok" : "warn"}` }),
      h("strong", {}, comp.group),
      h("span", { class: "spacer" }),
      h("span", { class: "hint mono" }, comp.uptime_pct.toFixed(3) + "% uptime" + " · 24h")));
    for (const item of comp.checks) {
      const barsEl = h("div", { class: "bars" }, (item.days || []).map((d) => h("i", { class: d.status, title: `${new Date(d.day * 1000).toLocaleDateString()} · ${d.uptime_pct ?? "no data"}%` })));
      group.appendChild(h("div", { class: "status-comp" },
        h("span", { class: `dot ${item.ok ? "ok" : item.ok === false ? "crit" : "muted"}` }),
        h("div", { class: "grow" }, h("div", {}, item.name), h("div", { class: "hint mono" }, item.url)),
        h("div", { class: "hint" }, barsEl),
        h("span", { class: "mono", style: { width: "76px", textAlign: "right" } }, item.latency_ms ? item.latency_ms.toFixed(0) + "ms" : "—")));
    }
    statusHost.appendChild(group);
  }
  if (status.incidents.length) {
    statusHost.appendChild(panel("Active incidents", h("div", {}, status.incidents.map((i) =>
      h("a", { class: "notif", href: `#/incidents` },
        h("span", { class: "n-time" }, i.reference),
        h("div", { class: "flex1" }, i.title, h("div", { class: "hint" }, i.status)),
        badge(i.severity, "crit"))), { pad: true })));
  }
  root.appendChild(h("div", { class: "section mt14" },
    h("div", { class: "section-head" }, h("h2", {}, "Status page"), h("span", { class: "hint" }, "what your customers see")),
    statusHost));
}

const kpi = (label, value, sub, tone = "") => h("div", { class: `kpi ${tone}` }, h("div", { class: "k-label" }, label), h("div", { class: "k-value" }, value), h("div", { class: "k-sub" }, sub));
const avgUptime = (checks) => checks.length ? checks.reduce((a, c) => a + c.uptime_24h.uptime_pct, 0) / checks.length : 100;
const medianLatency = (checks) => {
  const lat = checks.map((c) => c.last?.latency_ms).filter(Boolean).sort((a, b) => a - b);
  return lat.length ? Math.round(lat[Math.floor(lat.length / 2)]) : 0;
};

function openCheck(ctx, check) {
  const nameIn = h("input", { class: "input", value: check?.name || "" });
  const urlIn = h("input", { class: "input mono", value: check?.url || "https://example.com/health" });
  const methodIn = h("select", { class: "select" }, ["GET", "HEAD", "POST"].map((m) => h("option", { value: m, selected: check?.method === m }, m)));
  const expectIn = h("input", { class: "input", type: "number", value: check?.expect_status ?? 200 });
  const bodyIn = h("input", { class: "input", value: check?.expect_body || "", placeholder: "optional body substring" });
  const intervalIn = h("input", { class: "input", type: "number", value: check?.interval_s ?? 60 });
  const groupIn = h("input", { class: "input", value: check?.group_name || "Core" });
  const regionsIn = h("input", { class: "input mono", value: (check?.regions || ["iad", "fra"]).join(",") });
  const m = modal({
    title: check ? "Edit check" : "New synthetic check",
    body: [
      h("div", { class: "form-grid" }, formField("Name", nameIn), formField("Group", groupIn)),
      formField("URL", urlIn),
      h("div", { class: "form-grid" }, formField("Method", methodIn), formField("Expected status", expectIn), formField("Interval (s)", intervalIn)),
      formField("Response body contains", bodyIn),
      formField("Regions", regionsIn, "Comma separated labels (iad, fra, sin, sfo)"),
    ],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        if (!nameIn.value.trim()) return toast("Name required", "err");
        await ctx.api.saveCheck({
          name: nameIn.value.trim(), url: urlIn.value, method: methodIn.value, expect_status: Number(expectIn.value),
          expect_body: bodyIn.value, interval_s: Number(intervalIn.value), group: groupIn.value,
          regions: regionsIn.value.split(",").map((s) => s.trim()).filter(Boolean), enabled: true, public: true,
        });
        toast("Check saved");
        m.close();
        ctx.refresh();
      } }, "Save check"),
    ],
  });
}
