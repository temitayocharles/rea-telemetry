import { h, clear, panel, badge, stateBadge, empty, clock, ago, dur, num, tabs, toast, modal, formField, confirmDialog, levelBadge } from "../ui.js";
import { mount, timeSeries } from "../charts.js";

export async function render(root, ctx) {
  ctx.setTitle("Alerting");
  let tab = ctx.route.params.get("tab") || "active";
  const tabHost = h("div", {});
  const body = h("div", {});
  const alerts = await ctx.api.alerts();
  const firing = alerts.alerts.filter((a) => a.state === "firing");

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("h1", {}, "Alerting"),
        firing.length ? badge(`${firing.length} firing`, "crit") : badge("all clear", "ok")),
      h("p", { class: "sub" }, "PromQL rules with for-durations, grouping, silences and routed notifications.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("button", { class: "btn", onClick: async () => { const res = await ctx.api.evaluateAlerts(); toast(`Evaluated ${res.checked} rules · ${res.firing} firing`); ctx.refresh(); } }, "Evaluate now"),
      h("button", { class: "btn primary", onClick: () => openRule(ctx, null) }, "New alert rule"))));

  root.appendChild(tabHost);
  root.appendChild(body);

  function drawTabs() {
    clear(tabHost);
    tabHost.appendChild(tabs([
      { id: "active", label: `Active (${alerts.alerts.filter((a) => a.state !== "resolved").length})` },
      { id: "rules", label: "Rules" },
      { id: "silences", label: "Silences" },
      { id: "routing", label: "Routing" },
      { id: "history", label: "History" },
    ], tab, (id) => { tab = id; drawTabs(); drawBody(); }));
  }

  async function drawBody() {
    clear(body);
    if (tab === "active") return drawActive();
    if (tab === "rules") return drawRules();
    if (tab === "silences") return drawSilences();
    if (tab === "routing") return drawRouting();
    if (tab === "history") return drawHistory();
  }

  const alertRow = (a) => h("tr", {},
    h("td", {}, stateBadge(a.state)),
    h("td", {}, h("span", { class: `badge ${a.severity === "critical" ? "crit" : a.severity === "warning" ? "warn" : "info"}` }, a.severity)),
    h("td", {}, h("div", { style: { fontWeight: 550 } }, a.name), h("div", { class: "hint" }, a.summary)),
    h("td", {},
      h("div", { class: "chips" }, Object.entries(a.labels).map(([k, v]) => h("span", { class: "chip" }, `${k}=${v}`))),
      h("div", { class: "hint mono", style: { marginTop: "4px" } }, a.expr)),
    h("td", { class: "num" }, a.value !== null ? num(a.value, 3) : "—"),
    h("td", { class: "num" }, dur(a.duration_s)),
    h("td", { class: "num muted" }, ago(a.last_eval)),
    h("td", {}, h("div", { class: "row", style: { gap: "4px", justifyContent: "flex-end" } },
      a.state !== "resolved" ? h("button", { class: "btn sm", onClick: async () => { await ctx.api.ackAlert(a.id, "operator"); toast("Alert acknowledged"); ctx.refresh(); } }, a.acked_by ? "acked" : "Ack") : null,
      a.state === "firing" ? h("button", { class: "btn sm", onClick: async () => { const r = await ctx.api.alertToIncident(a.id); toast(`Declared ${r.reference}`); ctx.go(`/incidents/${r.id}`); } }, "Declare incident") : null,
      a.state !== "resolved" ? h("button", { class: "btn sm ghost", onClick: () => openSilence(ctx, a.labels) }, "Silence") : null)));

  async function drawActive() {
    const res = await ctx.api.alerts();
    const list = res.alerts;
    const ev = res.last_eval || {};
    body.appendChild(h("div", { class: "row", style: { alignItems: "center", gap: "10px", marginBottom: "10px" } },
      h("span", { class: "hint" }, ev.at ? `Last evaluated ${ago(ev.at)} · ${ev.checked} rules · ${ev.firing} firing` : "Not evaluated yet"),
      h("span", { class: "spacer" }),
      ev.errors?.length ? h("span", { class: "badge crit" }, `${ev.errors.length} rule errors`) : null));
    if (!list.length) { body.appendChild(empty("No alert instances", "Rules that match data will appear here.")); return; }
    const head = h("thead", {}, h("tr", {}, ["State", "Severity", "Alert", "Labels / query", "Value", "Duration", "Last eval", ""]
      .map((c, i) => h("th", { class: i >= 4 && i <= 6 ? "right" : "" }, c))));
    body.appendChild(panel("", h("div", { class: "table-wrap", style: { maxHeight: "62vh" } },
      h("table", { class: "data" }, head, h("tbody", {}, list.map(alertRow)))), { pad: false }));
  }

  const ruleRow = (r) => h("tr", {},
    h("td", {}, h("div", { style: { fontWeight: 550 } }, r.name), h("div", { class: "hint" }, r.summary)),
    h("td", { class: "mono truncate", style: { maxWidth: "360px" } }, r.expr),
    h("td", { class: "num" }, dur(r.for)),
    h("td", {}, h("span", { class: `badge ${r.severity === "critical" ? "crit" : "warn"}` }, r.severity)),
    h("td", {}, h("span", { class: "badge muted" }, r.group || "default")),
    h("td", {}, r.enabled ? badge("on", "ok") : badge("off", "muted")),
    h("td", { class: "right" }, h("div", { class: "row", style: { gap: "4px", justifyContent: "flex-end" } },
      h("button", { class: "btn sm", onClick: () => openRule(ctx, r) }, "Edit"),
      h("button", { class: "btn sm ghost", onClick: () => confirmDialog("Delete rule?", `${r.name} will stop being evaluated.`, async () => { await ctx.api.deleteRule(r.id); toast("Rule deleted"); ctx.refresh(); }, "Delete", true) }, "Delete"))));

  async function drawRules() {
    const { rules } = await ctx.api.rules();
    body.appendChild(h("div", { class: "row", style: { marginBottom: "10px" } },
      h("span", { class: "hint" }, `${rules.length} rules · evaluated by the collector every 15s`),
      h("span", { class: "spacer" }),
      h("button", { class: "btn sm", onClick: () => openRule(ctx, null) }, "+ New rule")));
    const head = h("thead", {}, h("tr", {}, ["Rule", "Expression", "For", "Severity", "Group", "Enabled", ""].map((c) => h("th", {}, c))));
    body.appendChild(panel("", h("table", { class: "data" }, head, h("tbody", {}, rules.map(ruleRow))), { pad: false }));
  }

  async function drawSilences() {
    const { silences } = await ctx.api.silences();
    body.appendChild(h("div", { class: "row", style: { marginBottom: "10px" } },
      h("span", { class: "hint" }, "Silences suppress matching alerts before routing."), h("span", { class: "spacer" }),
      h("button", { class: "btn sm primary", onClick: () => openSilence(ctx, {}) }, "+ New silence")));
    if (!silences.length) { body.appendChild(empty("No silences", "Create one to mute expected noise.")); return; }
    body.appendChild(panel("", h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["State", "Matchers", "Created by", "Comment", "Ends", ""].map((c) => h("th", {}, c)))),
      h("tbody", {}, silences.map((s) => h("tr", {},
        h("td", {}, stateBadge(s.state)),
        h("td", {}, h("div", { class: "chips" }, s.matchers.map((m) => h("span", { class: "chip" }, `${m.name}${m.op || "="}"${m.value}"`)))),
        h("td", {}, s.created_by),
        h("td", { class: "muted" }, s.comment || "—"),
        h("td", { class: "num" }, s.state === "expired" ? "expired" : ago(s.ends_at).replace(" ago", "")),
        h("td", { class: "right" }, h("button", { class: "btn sm ghost", onClick: async () => { await ctx.api.deleteSilence(s.id); toast("Silence removed"); ctx.refresh(); } }, "Expire")))))), { pad: false }));
  }

  async function drawRouting() {
    const [receivers, notifications, oncall, schedules] = await Promise.all([
      ctx.api.receivers(), ctx.api.notifications(), ctx.api.onCall(), ctx.api.schedules(),
    ]);
    const recvHost = h("div", {});
    const drawRecv = () => {
      clear(recvHost);
      for (const r of receivers.receivers) {
        const urlInput = h("input", { class: "input mono", value: r.url || "", placeholder: "https://hooks.slack.com/services/…" });
        recvHost.appendChild(h("div", { class: "pipeline" },
          h("div", {}, h("div", { style: { fontWeight: 550 } }, r.name), h("div", { class: "hint" }, r.kind)),
          h("div", { class: "row", style: { alignItems: "center" } }, urlInput,
            h("button", { class: "btn sm", onClick: async () => {
              r.url = urlInput.value;
              await ctx.api.saveReceivers(receivers.receivers);
              toast("Receiver saved");
            } }, "Save"),
            h("button", { class: "btn sm ghost", onClick: async () => { const res = await ctx.api.testReceiver(r.name); toast(`Test sent to ${res.receiver}`); ctx.refresh(); } }, "Send test"))));
      }
    };
    drawRecv();
    body.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap" } },
      h("div", { style: { flex: "1 1 460px" } }, panel("Receivers", recvHost, { pad: true })),
      h("div", { style: { flex: "1 1 320px" } }, panel("On-call rotations", h("div", {}, oncall.on_call.map((o) =>
        h("div", { class: "oncall" },
          h("div", { class: "avatar" }, o.on_call.split(" ").map((x) => x[0]).join("")),
          h("div", { class: "oc-body" }, o.on_call, h("small", {}, `${o.schedule} · ${o.rotation.replace(/_/g, " ")}`)),
          h("span", { class: "badge muted" }, o.members.length + " people")))), { pad: true }))));
    body.appendChild(h("div", { class: "mt14" }, panel("Recent notifications", h("div", {}, notifications.notifications.slice(0, 25).map((n) =>
      h("div", { class: "notif" },
        h("span", { class: "n-time" }, clock(n.ts, true)),
        h("div", { class: "flex1" }, h("div", { style: { fontWeight: 500 } }, n.title), h("div", { class: "hint" }, `${n.receiver} · ${n.kind}`)),
        h("span", { class: `delivery ${n.status.split(" ")[0]}` }, n.status)))), { pad: true })));
  }

  async function drawHistory() {
    const res = await ctx.api.alerts();
    const resolved = res.alerts.filter((a) => a.state === "resolved");
    if (!resolved.length) { body.appendChild(empty("No history yet", "Resolved alerts are kept here.")); return; }
    body.appendChild(panel("Resolved alerts", h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["Alert", "Severity", "Labels", "Fired", "Resolved", "Duration", "Acked by"].map((c) => h("th", {}, c)))),
      h("tbody", {}, resolved.map((a) => h("tr", {},
        h("td", {}, a.name),
        h("td", {}, h("span", { class: `badge ${a.severity === "critical" ? "crit" : "warn"}` }, a.severity)),
        h("td", {}, h("div", { class: "chips" }, Object.entries(a.labels).map(([k, v]) => h("span", { class: "chip" }, `${k}=${v}`)))),
        h("td", { class: "mono muted" }, clock(a.active_since, true)),
        h("td", { class: "mono muted" }, a.active_since + a.duration_s ? clock(a.active_since + a.duration_s, true) : "—"),
        h("td", { class: "num" }, dur(a.duration_s)),
        h("td", {}, a.acked_by || "—"))))), { pad: false }));
  }

  drawTabs();
  await drawBody();
}

function openRule(ctx, rule) {
  const nameIn = h("input", { class: "input", value: rule?.name || "", placeholder: "HighErrorRate" });
  const exprIn = h("textarea", { class: "textarea", value: rule?.expr || 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (service) / sum(rate(http_requests_total[5m])) by (service) > 0.02' });
  const forIn = h("input", { class: "input", type: "number", value: rule?.for ?? 300, min: 0 });
  const sevIn = h("select", { class: "select" }, ["critical", "warning", "info"].map((s) => h("option", { value: s, selected: rule?.severity === s }, s)));
  const summaryIn = h("input", { class: "input", value: rule?.summary || "" });
  const groupIn = h("input", { class: "input", value: rule?.group || "default" });
  const runbookIn = h("input", { class: "input", value: rule?.runbook || "" });
  const preview = h("div", {});
  const m = modal({
    title: rule ? "Edit alert rule" : "New alert rule",
    wide: true,
    body: [
      h("div", { class: "form-grid" }, formField("Name", nameIn), formField("Severity", sevIn), formField("For (seconds)", forIn), formField("Group", groupIn)),
      formField("PromQL expression", exprIn, "A non-empty result means the alert is active. Values are attached to the alert."),
      h("div", { class: "form-grid" }, formField("Summary", summaryIn), formField("Runbook URL", runbookIn)),
      h("div", { class: "row" }, h("button", { class: "btn sm", onClick: async () => {
        clear(preview);
        const res = await ctx.api.previewRule(exprIn.value);
        if (!res.ok) { preview.appendChild(h("div", { class: "badge crit" }, res.error)); return; }
        const count = (res.result.result || []).length;
        preview.appendChild(h("div", { class: `badge ${count ? "warn" : "ok"}` }, count ? `${count} series would fire` : "no series matched at this instant"));
      } }, "Test expression"), h("span", { class: "hint" }, "Evaluates right now against stored data")),
      preview,
    ],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        if (!nameIn.value.trim()) return toast("Name is required", "err");
        try {
          await ctx.api.saveRule({ name: nameIn.value.trim(), expr: exprIn.value, for: Number(forIn.value), severity: sevIn.value, summary: summaryIn.value, group: groupIn.value, runbook: runbookIn.value, enabled: true });
          toast("Rule saved");
          m.close();
          ctx.refresh();
        } catch (err) { toast(err.message, "err"); }
      } }, "Save rule"),
    ],
  });
}

function openSilence(ctx, labels) {
  const defaults = Object.entries(labels || {}).filter(([k]) => k === "service" || k === "alertname" || k === "severity");
  const rows = (defaults.length ? defaults : [["service", ""]]).map(([k, v]) => ({ name: k, op: "=", value: v }));
  const host = h("div", { class: "stack" });
  const draw = () => {
    clear(host);
    rows.forEach((row, i) => {
      const nameIn = h("select", { class: "select" }, ["service", "alertname", "severity", "job", "instance", "namespace", "group"].map((n) => h("option", { value: n, selected: row.name === n }, n)));
      const opIn = h("select", { class: "select" }, ["=", "!=", "=~", "!~"].map((o) => h("option", { value: o, selected: row.op === o }, o)));
      const valIn = h("input", { class: "input mono", value: row.value || "", placeholder: "value" });
      nameIn.onchange = () => (row.name = nameIn.value);
      opIn.onchange = () => (row.op = opIn.value);
      valIn.oninput = () => (row.value = valIn.value);
      host.appendChild(h("div", { class: "row", style: { gap: "6px" } }, nameIn, opIn, valIn,
        h("button", { class: "btn sm ghost", onClick: () => { rows.splice(i, 1); draw(); } }, "×")));
    });
    host.appendChild(h("button", { class: "btn sm ghost", onClick: () => { rows.push({ name: "service", op: "=", value: "" }); draw(); } }, "+ Add matcher"));
  };
  draw();
  const durationIn = h("select", { class: "select" }, [["30m", 1800], ["1h", 3600], ["4h", 14400], ["12h", 43200], ["24h", 86400], ["7d", 604800]].map(([l, v]) => h("option", { value: v, selected: v === 7200 }, l)));
  const commentIn = h("input", { class: "input", placeholder: "Why is this silenced?" });
  const m = modal({
    title: "Create silence",
    body: [host, h("div", { class: "form-grid" }, formField("Duration", durationIn), formField("Comment", commentIn))],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        await ctx.api.createSilence({ matchers: rows.filter((r) => r.value), duration_s: Number(durationIn.value), created_by: "operator", comment: commentIn.value });
        toast("Silence created");
        m.close();
        ctx.refresh();
      } }, "Silence"),
    ],
  });
}
