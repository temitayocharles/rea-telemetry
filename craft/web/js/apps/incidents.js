import { h, clear, panel, badge, sevBadge, stateBadge, empty, clock, ago, dur, toast, modal, formField, confirmDialog, initials } from "../ui.js";

const STATUSES = ["investigating", "identified", "monitoring", "resolved"];

export async function render(root, ctx) {
  if (ctx.route.parts[1]) return renderDetail(root, ctx, Number(ctx.route.parts[1]));
  ctx.setTitle("Incidents");
  const { incidents } = await ctx.api.incidents();
  const open = incidents.filter((i) => !i.resolved_at);
  const oncall = (await ctx.api.onCall()).on_call;

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } }, h("h1", {}, "Incidents"), open.length ? badge(`${open.length} active`, "crit") : badge("none active", "ok")),
      h("p", { class: "sub" }, "Declare, coordinate and review. Every incident carries a timeline and a postmortem.")),
    h("button", { class: "btn primary", onClick: () => openDeclare(ctx) }, "Declare incident")));

  if (open.length) {
    root.appendChild(h("div", { class: "section" },
      h("div", { class: "section-head" }, h("h2", {}, "Active")),
      h("div", { class: "stack" }, open.map((i) => incidentCard(ctx, i, true)))));
  }

  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "All incidents"), h("span", { class: "hint" }, `${incidents.length} total`)),
    incidents.length ? panel("", h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["Reference", "Incident", "Severity", "Status", "Commander", "Duration", "Opened"].map((c) => h("th", {}, c)))),
      h("tbody", {}, incidents.map((i) => h("tr", { class: "clickable", onClick: () => ctx.go(`/incidents/${i.id}`) },
        h("td", { class: "mono" }, i.reference),
        h("td", {}, h("div", { style: { fontWeight: 500 } }, i.title), h("div", { class: "hint" }, i.services.join(", "))),
        h("td", {}, sevBadge(i.severity)),
        h("td", {}, stateBadge(i.status)),
        h("td", {}, i.commander || "—"),
        h("td", { class: "num" }, dur(i.duration_s)),
        h("td", { class: "num muted" }, ago(i.created_at)))))), { pad: false })
      : empty("No incidents", "When it hits the fan, declare here.")
  ));

  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Responders on call")),
    panel("", h("div", { style: { padding: "4px 14px" } }, oncall.map((o) =>
      h("div", { class: "oncall" },
        h("div", { class: "avatar" }, initials(o.on_call)),
        h("div", { class: "oc-body" }, o.on_call, h("small", {}, `${o.schedule} · ${o.rotation.replace(/_/g, " ")}`)),
        h("span", { class: "hint" }, "until " + clock(o.next_handoff, true))))), { pad: false })));
}

function incidentCard(ctx, i, active) {
  return h("div", { class: "panel panel-pad", style: { cursor: "pointer", borderColor: active ? "#4a2626" : undefined }, onClick: () => ctx.go(`/incidents/${i.id}`) },
    h("div", { class: "row", style: { alignItems: "center", gap: "9px" } },
      sevBadge(i.severity), stateBadge(i.status), h("strong", { style: { fontSize: "13.5px" } }, i.title), h("span", { class: "spacer" }),
      h("span", { class: "hint mono" }, i.reference)),
    h("div", { class: "row mt8", style: { gap: "18px", flexWrap: "wrap" } },
      h("span", { class: "hint" }, `IC: ${i.commander || "unassigned"}`),
      h("span", { class: "hint" }, `${i.responders.length} responders`),
      h("span", { class: "hint" }, `duration ${dur(i.duration_s)}`),
      h("span", { class: "hint" }, i.services.join(", "))));
}

function openDeclare(ctx) {
  const titleIn = h("input", { class: "input", placeholder: "Checkout failing on payment authorization" });
  const sevIn = h("select", { class: "select" }, ["SEV1", "SEV2", "SEV3", "SEV4"].map((s) => h("option", { value: s, selected: s === "SEV2" }, s)));
  const impactIn = h("select", { class: "select" }, ["critical", "major", "minor", "none"].map((s) => h("option", { value: s, selected: s === "major" }, s)));
  const commanderIn = h("input", { class: "input", placeholder: "Incident commander" });
  const servicesIn = h("input", { class: "input mono", placeholder: "checkout-service, payment-service" });
  const summaryIn = h("textarea", { class: "textarea", placeholder: "What is the customer impact right now?" });
  const m = modal({
    title: "Declare incident",
    wide: true,
    body: [
      formField("Title", titleIn),
      h("div", { class: "form-grid" }, formField("Severity", sevIn), formField("Impact", impactIn), formField("Commander", commanderIn)),
      formField("Affected services", servicesIn, "Comma separated"),
      formField("Customer impact", summaryIn),
    ],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        if (!titleIn.value.trim()) return toast("A title is required", "err");
        const res = await ctx.api.declareIncident({
          title: titleIn.value, severity: sevIn.value, impact: impactIn.value, commander: commanderIn.value,
          services: servicesIn.value.split(",").map((s) => s.trim()).filter(Boolean), summary: summaryIn.value,
        });
        m.close();
        toast(`${res.reference} declared`);
        ctx.go(`/incidents/${res.id}`);
      } }, "Declare"),
    ],
  });
}

async function renderDetail(root, ctx, id) {
  const inc = await ctx.api.incident(id);
  ctx.setTitle(`${inc.reference} · ${inc.title}`);
  const update = async () => { ctx.refresh(); };

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("button", { class: "btn ghost sm", onClick: () => ctx.go("/incidents") }, "←"),
        sevBadge(inc.severity), stateBadge(inc.status), h("span", { class: "mono muted" }, inc.reference)),
      h("h1", { style: { marginTop: "6px" } }, inc.title),
      h("p", { class: "sub" }, inc.summary || "No summary yet.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("select", { class: "select", style: { width: "auto" }, onChange: async (e) => {
        await ctx.api.incidentAction(id, "status", { status: e.target.value, actor: inc.commander || "operator" });
        toast(`Status → ${e.target.value}`);
        update();
      } }, STATUSES.map((s) => h("option", { value: s, selected: s === inc.status }, s))),
      inc.status !== "resolved" ? h("button", { class: "btn primary", onClick: async () => { await ctx.api.incidentAction(id, "status", { status: "resolved", actor: inc.commander }); toast("Incident resolved"); update(); } }, "Resolve") : null)));

  root.appendChild(h("div", { class: "kpi-grid mb14" },
    kpi("Commander", inc.commander || "unassigned", "incident commander"),
    kpi("Responders", inc.responders.length, inc.responders.slice(0, 3).join(", ")),
    kpi("Duration", dur(inc.duration_s), inc.resolved_at ? "resolved" : "ongoing"),
    kpi("Impact", inc.impact, "customer impact"),
    kpi("Services", inc.services.length, inc.services.join(", "))));

  const commentIn = h("input", { class: "input", placeholder: "Post an update to the timeline…" });
  const post = async () => {
    if (!commentIn.value.trim()) return;
    await ctx.api.incidentAction(id, "events", { kind: "comment", actor: inc.commander || "operator", body: commentIn.value });
    commentIn.value = "";
    toast("Update posted");
    update();
  };
  commentIn.addEventListener("keydown", (e) => { if (e.key === "Enter") post(); });

  const timeline = h("div", { class: "timeline" }, inc.timeline.map((e) =>
    h("div", { class: `tl-item ${e.kind}` },
      h("div", { class: "tl-meta" }, h("span", {}, clock(e.ts, true)), h("span", {}, e.actor || "system"), h("span", { class: "badge muted" }, e.kind)),
      h("div", { class: "tl-body" }, e.body))));

  const pm = inc.postmortem || {};
  const pmHost = h("div", {});
  const actionsHost = h("div", {});
  let actions = pm.action_items || [];
  const drawPm = () => {
    clear(pmHost);
    if (!pm.author) {
      pmHost.appendChild(h("div", { class: "stack" },
        h("p", { class: "sub", style: { margin: 0 } }, "No postmortem yet. Start a blameless review while the timeline is fresh."),
        h("button", { class: "btn primary", onClick: () => startPostmortem() }, "Start postmortem")));
      return;
    }
    pmHost.appendChild(h("div", { class: "stack" },
      h("div", { class: "row", style: { gap: "8px" } }, badge(pm.status || "draft", "info"), h("span", { class: "hint" }, `author ${pm.author}`)),
      pmField("Summary", pm.summary),
      pmField("Customer impact", pm.impact),
      pmField("Root cause", pm.root_cause),
      h("div", {}, h("div", { class: "k-label", style: { fontSize: "10px", color: "var(--text-3)", textTransform: "uppercase", marginBottom: "4px" } }, "Action items"), actionsHost),
      h("button", { class: "btn sm", onClick: () => startPostmortem(pm) }, "Edit postmortem")));
    clear(actionsHost);
    (actions || []).forEach((a) => actionsHost.appendChild(h("label", { class: "action-item" },
      h("input", { type: "checkbox", checked: a.done, onChange: async (e) => { a.done = e.target.checked; await ctx.api.incidentAction(id, "postmortem", { ...pm, action_items: actions }); toast("Action updated"); } }),
      h("span", { class: "flex1" }, a.item),
      h("span", { class: "hint mono" }, `${a.owner} · ${a.due || "no date"}`))));
  };

  const startPostmortem = (existing) => {
    const summaryIn = h("textarea", { class: "textarea" }, existing?.summary || "");
    const impactIn = h("textarea", { class: "textarea" }, existing?.impact || "");
    const causeIn = h("textarea", { class: "textarea" }, existing?.root_cause || "");
    const aiIn = h("textarea", { class: "textarea", placeholder: "One action per line: owner — action — due date" }, (existing?.action_items || []).map((a) => `${a.owner} — ${a.item} — ${a.due || ""}`).join("\n"));
    const m = modal({
      title: "Postmortem",
      wide: true,
      body: [
        formField("Summary", summaryIn),
        formField("Customer impact", impactIn),
        formField("Root cause", causeIn),
        formField("Action items", aiIn, "Format: owner — action — due date"),
      ],
      footer: [
        h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
        h("button", { class: "btn primary", onClick: async () => {
          const items = aiIn.value.split("\n").filter(Boolean).map((line) => {
            const [owner, item, due] = line.split("—").map((s) => s.trim());
            return { owner: owner || "unassigned", item: item || line, due, done: false };
          });
          actions = items;
          await ctx.api.incidentAction(id, "postmortem", { author: inc.commander || "operator", status: "in_review", summary: summaryIn.value, impact: impactIn.value, root_cause: causeIn.value, action_items: items });
          m.close();
          toast("Postmortem saved");
          update();
        } }, "Save postmortem"),
      ],
    });
  };

  const pmField = (label, value) => h("div", {}, h("div", { class: "k-label", style: { fontSize: "10px", color: "var(--text-3)", textTransform: "uppercase", marginBottom: "3px" } }, label), h("div", { style: { fontSize: "12.5px", color: value ? "var(--text)" : "var(--text-3)" } }, value || "—"));

  root.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap" } },
    h("div", { style: { flex: "1.4 1 440px", minWidth: "320px" } },
      panel("Timeline", h("div", {},
        h("div", { class: "row mb14" }, commentIn, h("button", { class: "btn primary", onClick: post }, "Post")),
        timeline), { pad: true })),
    h("div", { style: { flex: "1 1 320px", minWidth: "280px" } },
      panel("Postmortem", pmHost, { pad: true }),
      h("div", { class: "mt14" }, panel("Responders", h("div", {},
        inc.responders.map((p) => h("div", { class: "oncall" }, h("div", { class: "avatar" }, initials(p)), h("div", { class: "oc-body" }, p, h("small", {}, p === inc.commander ? "incident commander" : "responder")))),
        h("div", { class: "row mt8" }, h("input", { class: "input", placeholder: "Add responder", id: "add-responder" }),
          h("button", { class: "btn", onClick: async () => {
            const input = document.getElementById("add-responder");
            if (!input.value.trim()) return;
            await ctx.api.incidentAction(id, "responders", { person: input.value.trim() });
            toast("Responder added"); update();
          } }, "Add")), ), { pad: true })),
      h("div", { class: "mt14" }, panel("Related dashboards", h("div", { class: "stack" },
        h("a", { class: "link", href: "#/dashboards/service-overview" }, "Service Overview →"),
        h("a", { class: "link", href: "#/logs" }, "Search logs for this window →"),
        h("a", { class: "link", href: "#/alerts" }, "Alerts that fired →")), { pad: true })))));

  drawPm();
}

function kpi(label, value, sub) {
  return h("div", { class: "kpi" }, h("div", { class: "k-label" }, label), h("div", { class: "k-value", style: { fontSize: "16px" } }, value), h("div", { class: "k-sub" }, sub));
}
