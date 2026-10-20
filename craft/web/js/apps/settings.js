import { h, clear, panel, badge, empty, bytes, num, toast, ago, clock, modal, formField, confirmDialog, copy, initials } from "../ui.js";

export async function render(root, ctx) {
  ctx.setTitle("Settings");
  const settings = await ctx.api.settings();
  const org = settings.org || {};
  const retain = settings.retention_days || 30;
  const collector = settings.collector || {};

  const orgName = h("input", { class: "input", value: org.name || "Acme Commerce" });
  const retention = h("input", { class: "input", type: "number", value: retain, min: 1, max: 365 });
  const receiverHost = h("div", {});
  let receivers = [...(settings.receivers || [])];

  const drawReceivers = () => {
    clear(receiverHost);
    receivers.forEach((r, i) => {
      const urlIn = h("input", { class: "input mono", value: r.url || "", placeholder: r.kind === "slack" ? "https://hooks.slack.com/services/…" : "https://…" });
      const enabled = h("input", { type: "checkbox", checked: r.enabled !== false });
      urlIn.oninput = () => (r.url = urlIn.value);
      enabled.onchange = () => (r.enabled = enabled.checked);
      receiverHost.appendChild(h("div", { class: "pipeline" },
        h("div", {}, h("div", { style: { fontWeight: 550 } }, r.name), h("div", { class: "hint" }, r.kind)),
        h("div", { class: "row", style: { alignItems: "center" } }, urlIn,
          h("label", { class: "row", style: { gap: "5px", alignItems: "center", fontSize: "11px", color: "var(--text-3)", whiteSpace: "nowrap" } }, enabled, "enabled"),
          h("button", { class: "btn sm", onClick: async () => { await ctx.api.saveReceivers(receivers); toast("Receivers saved"); } }, "Save"),
          h("button", { class: "btn sm ghost", onClick: async () => { const r2 = await ctx.api.testReceiver(r.name); toast(`Test delivered to ${r2.receiver}`); } }, "Test"))));
    });
  };
  drawReceivers();

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Settings"), h("p", { class: "sub" }, "Organisation, alert routing, retention and demo data."))));

  root.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap" } },
    h("div", { style: { flex: "1 1 360px", minWidth: "300px" } },
      panel("Organisation", h("div", { class: "stack" },
        formField("Name", orgName),
        h("div", { class: "form-grid" }, formField("Retention (days)", retention, "Data older than this is pruned hourly.")),
        h("div", { class: "row" }, h("button", { class: "btn primary", onClick: async () => {
          await ctx.api.saveSettings({ org: { ...org, name: orgName.value }, retention_days: Number(retention.value) });
          toast("Settings saved");
          ctx.refresh();
        } }, "Save changes")),
      ), { pad: true }),
      h("div", { class: "mt14" }, panel("Notification receivers", h("div", {}, receiverHost,
        h("div", { class: "mt8" }, h("button", { class: "btn sm", onClick: async () => { await ctx.api.saveReceivers(receivers); toast("Saved"); } }, "Save all"))), { pad: true })),
      h("div", { class: "mt14" }, panel("On-call schedules", h("div", {}, settings.on_call.map((o) =>
        h("div", { class: "oncall" },
          h("div", { class: "avatar" }, initials(o.on_call)),
          h("div", { class: "oc-body" }, o.schedule, h("small", {}, `${o.rotation.replace(/_/g, " ")} · ${o.members.join(", ")}`)),
          h("span", { class: "badge ok" }, o.on_call)))), { pad: true }))),
    h("div", { style: { flex: "1 1 360px", minWidth: "300px" } },
      panel("Data & storage", h("div", { class: "stack" },
        row("Engine", "SQLite WAL · " + (settings.storage?.engine || "")),
        row("Series", num(settings.storage?.series || 0)),
        row("Samples", num(settings.storage?.samples || 0)),
        row("Logs", num(settings.storage?.logs || 0)),
        row("Spans", num(settings.storage?.spans || 0)),
        row("On disk", bytes(settings.storage?.db_bytes || 0)),
        h("div", { class: "row mt8", style: { gap: "6px" } },
          h("button", { class: "btn", onClick: async () => { const r = await ctx.api.prune(); toast(`Pruned ${num(r.samples)} samples`); ctx.refresh(); } }, "Prune now"),
          h("button", { class: "btn", onClick: async () => {
            const data = await ctx.api.exportAll();
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            const a = document.createElement("a");
            a.href = URL.createObjectURL(blob);
            a.download = `telemetrycraft-export-${new Date().toISOString().slice(0, 10)}.json`;
            a.click();
            URL.revokeObjectURL(a.href);
            toast("Export downloaded");
          } }, "Export JSON"),
          h("button", { class: "btn", onClick: () => copy(location.origin + "/api/prom/api/v1/write") }, "Copy remote-write URL"))), { pad: true }),
      h("div", { class: "mt14" }, panel("Demo data", h("div", { class: "stack" },
        h("p", { class: "sub", style: { margin: 0 } }, "The environment is generated locally: 7 days of series at two resolutions, correlated logs and traces, an active incident, synthetic history and error budgets."),
        h("div", { class: "row", style: { gap: "6px" } },
          h("button", { class: "btn", onClick: async () => { await ctx.api.seedDemo(false); toast("Demo already present"); } }, "Check status"),
          h("button", { class: "btn danger", onClick: () => confirmDialog("Regenerate demo data?", "This replaces all stored telemetry and configuration with a freshly generated environment.", async () => {
            toast("Regenerating… this takes a few seconds");
            await ctx.api.seedDemo(true);
            toast("Demo data regenerated");
            setTimeout(() => location.reload(), 500);
          }, "Regenerate", true) }, "Regenerate"))), { pad: true })),
      h("div", { class: "mt14" }, panel("About", h("div", { class: "stack sub" },
        h("div", { class: "row" }, h("span", {}, "Version"), h("span", { class: "spacer" }), h("span", { class: "mono" }, ctx.meta?.version || "1.0.0")),
        h("div", { class: "row" }, h("span", {}, "Uptime"), h("span", { class: "spacer" }), h("span", { class: "mono" }, ago(Math.floor(Date.now() / 1000) - (ctx.meta?.uptime_s || 0)))),
        h("div", { class: "row" }, h("span", {}, "Apps"), h("span", { class: "spacer" }), h("span", { class: "mono" }, (ctx.meta?.apps || []).length || 13)),
        h("div", { class: "row" }, h("span", {}, "License"), h("span", { class: "spacer" }), h("span", { class: "mono" }, "MIT")),
        h("div", { class: "row" }, h("span", {}, "Local-first"), h("span", { class: "spacer" }), h("span", { class: "mono" }, "no cloud round-trips"))), { pad: true })))));

  const appsHost = h("div", { class: "dash-cards" });
  for (const app of ctx.meta?.apps || []) {
    appsHost.appendChild(h("a", { class: "panel dash-card", href: "#" + app.path.replace(/^\/#/, ""), style: { textDecoration: "none", color: "inherit" } },
      h("h3", {}, app.name), h("div", { class: "meta" }, app.desc), h("div", { class: "hint mt8 mono" }, app.path)));
  }
  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Every app in the suite"), h("span", { class: "hint" }, "one console, one database")),
    appsHost));
}

const row = (k, v) => h("div", { class: "row", style: { fontSize: "12px" } }, h("span", { class: "muted" }, k), h("span", { class: "spacer" }), h("span", { class: "mono" }, v));
