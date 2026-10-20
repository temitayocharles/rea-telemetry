import { h, clear, panel, badge, levelBadge, empty, clock, ago, num, toast, modal, formField, confirmDialog } from "../ui.js";
import { sparkline, mount } from "../charts.js";

export async function render(root, ctx) {
  if (ctx.route.parts[1]) return renderIssue(root, ctx, ctx.route.parts[1]);
  ctx.setTitle("Errors");
  let status = ctx.route.params.get("status") || "unresolved";
  const listHost = h("div", {});
  const kpiHost = h("div", { class: "kpi-grid mb14" });
  const tabsHost = h("div", {});

  async function load() {
    clear(listHost);
    listHost.appendChild(h("div", { class: "loading" }, h("div", { class: "spinner" })));
    try {
      const [all, unresolved] = await Promise.all([ctx.api.issues({}), ctx.api.issues({ status: "unresolved" })]);
      const totalEvents = all.issues.reduce((a, b) => a + b.count, 0);
      const events24 = all.issues.reduce((a, b) => a + b.events_24h, 0);
      const users = all.issues.reduce((a, b) => a + b.users, 0);
      const resolved = all.issues.filter((i) => i.status === "resolved").length;
      clear(kpiHost);
      kpiHost.append(
        stat("Unresolved", unresolved.issues.length, unresolved.issues.length ? "needs triage" : "all triaged", unresolved.issues.length ? "bad" : ""),
        stat("Events (24h)", num(events24), "error events captured"),
        stat("Users affected", num(users), "distinct users"),
        stat("Total events", num(totalEvents), "lifetime"),
        stat("Resolved", resolved, "closed groups")
      );
      const issues = status === "all" ? all.issues : all.issues.filter((i) => i.status === status);
      clear(listHost);
      if (!issues.length) { listHost.appendChild(empty("Nothing here", "No issues with this status.")); return; }
      const rows = issues.map((i) => h("tr", { class: "clickable", onClick: () => ctx.go(`/errors/${i.id}`) },
        h("td", {},
          h("div", { class: "row", style: { alignItems: "center", gap: "7px" } },
            h("span", { class: `dot ${i.level === "fatal" || i.level === "error" ? "crit" : "warn"}` }),
            h("div", {}, h("div", { style: { fontWeight: 500 } }, i.title), h("div", { class: "hint mono" }, i.culprit || i.service))),
          h("div", { class: "chips", style: { marginTop: "4px" } }, Object.entries(i.tags).slice(0, 4).map(([k, v]) => h("span", { class: "chip" }, `${k}: ${v}`)))),
        h("td", {}, levelBadge(i.level)),
        h("td", { style: { width: "110px" } }, sparkline(i.sparkline, { width: 100, height: 24, color: "#ff8f8f", bars: true, barColor: "#a04a4a" })),
        h("td", { class: "num" }, num(i.events_24h)),
        h("td", { class: "num" }, num(i.count)),
        h("td", { class: "num" }, num(i.users)),
        h("td", { class: "num muted" }, ago(i.last_seen)),
        h("td", {}, i.status === "unresolved" ? badge("open", "crit") : i.status === "resolved" ? badge("resolved", "ok") : badge(i.status, "muted")),
        h("td", { class: "mono muted", style: { fontSize: "11px" } }, i.assigned || "—")));
      listHost.appendChild(panel("", h("div", { class: "table-wrap", style: { maxHeight: "62vh" } }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, ["Issue", "Level", "24h", "Events", "Total", "Users", "Last seen", "Status", "Owner"].map((c, idx) => h("th", { class: idx >= 2 && idx <= 5 ? "right" : "" }, c)))),
        h("tbody", {}, rows))), { pad: false }));
    } catch (err) {
      clear(listHost);
      listHost.appendChild(empty("Could not load issues", err.message));
    }
  }

  const stat = (label, value, sub, tone = "") => h("div", { class: `kpi ${tone}` },
    h("div", { class: "k-label" }, label), h("div", { class: "k-value" }, value), h("div", { class: "k-sub" }, sub));

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Errors"), h("p", { class: "sub" }, "Grouped exceptions with stack traces, affected users and releases — Sentry-style triage.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("button", { class: "btn", onClick: () => ctx.refresh() }, "Refresh"),
      h("a", { class: "btn", href: "#/collector" }, "Send events"))));
  root.appendChild(kpiHost);
  root.appendChild(tabsHost);
  root.appendChild(listHost);

  const drawTabs = () => {
    clear(tabsHost);
    tabsHost.appendChild(h("div", { class: "tabs" }, [["unresolved", "Unresolved"], ["resolved", "Resolved"], ["ignored", "Ignored"], ["all", "All"]].map(([id, label]) =>
      h("button", { class: `tab ${status === id ? "active" : ""}`, onClick: () => { status = id; drawTabs(); load(); } }, label))));
  };
  drawTabs();
  await load();
}

async function renderIssue(root, ctx, id) {
  const issue = await ctx.api.issue(Number(id));
  ctx.setTitle(issue.title.slice(0, 48));
  const levelTone = issue.level === "fatal" || issue.level === "error" ? "crit" : "warn";

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" },
      h("div", { class: "row", style: { alignItems: "center", gap: "8px" } },
        h("button", { class: "btn ghost sm", onClick: () => ctx.go("/errors") }, "←"),
        levelBadge(issue.level),
        issue.status === "unresolved" ? badge("open", "crit") : badge(issue.status, "ok")),
      h("h1", { style: { marginTop: "6px" } }, issue.title),
      h("p", { class: "sub mono" }, issue.culprit || issue.service)),
    h("div", { class: "row", style: { gap: "6px" } },
      h("button", { class: "btn", onClick: async () => { await ctx.api.issueStatus(issue.id, issue.status === "resolved" ? "unresolved" : "resolved"); toast("Issue updated"); ctx.refresh(); } },
        issue.status === "resolved" ? "Reopen" : "Resolve"),
      h("button", { class: "btn", onClick: async () => { await ctx.api.issueStatus(issue.id, "ignored"); toast("Issue ignored"); ctx.go("/errors"); } }, "Ignore"),
      h("button", { class: "btn", onClick: () => {
        const input = h("input", { class: "input", value: issue.assigned, placeholder: "name or team" });
        const m = modal({ title: "Assign issue", body: [formField("Owner", input)], footer: [
          h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
          h("button", { class: "btn primary", onClick: async () => { await ctx.api.issueAssign(issue.id, input.value); m.close(); toast("Assigned"); ctx.refresh(); } }, "Assign"),
        ] });
      } }, "Assign"))));

  root.appendChild(h("div", { class: "kpi-grid mb14" },
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Events"), h("div", { class: "k-value" }, num(issue.count))),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Users"), h("div", { class: "k-value" }, num(issue.users))),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "First seen"), h("div", { class: "k-value", style: { fontSize: "14px" } }, new Date(issue.first_seen * 1000).toLocaleString())),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Last seen"), h("div", { class: "k-value", style: { fontSize: "14px" } }, ago(issue.last_seen))),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Release"), h("div", { class: "k-value", style: { fontSize: "14px" } }, issue.release || "—")),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Owner"), h("div", { class: "k-value", style: { fontSize: "14px" } }, issue.assigned || "unassigned"))));

  const sparkHost = h("div", { class: "panel panel-pad mb14" });
  const sparkChart = h("div", {});
  sparkHost.append(h("div", { class: "row mb8" }, h("h3", {}, "Events over 24h"), h("span", { class: "spacer" }), h("span", { class: "hint" }, "hourly buckets")), sparkChart);
  root.appendChild(sparkHost);
  mount(sparkChart, () => {
    const el = h("div", {});
    sparkChart.appendChild(el);
    const bars = h("div", { class: "bars", style: { height: "52px", gap: "3px" } });
    const max = Math.max(...issue.sparkline, 1);
    el.appendChild(bars);
    issue.sparkline.forEach((v) => bars.appendChild(h("i", { style: { flex: "1", height: Math.max(3, (v / max) * 100) + "%" }, title: `${v} events` })));
  });

  root.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap" } },
    h("div", { style: { flex: "1.6 1 480px", minWidth: "320px" } }, panel("Stack trace", h("div", {}, issue.stack.length
      ? issue.stack.map((f, i) => h("div", { style: { padding: "7px 0", borderBottom: "1px solid var(--line-soft)", fontFamily: "var(--mono)", fontSize: "11.5px" } },
          h("div", { class: "row", style: { gap: "8px", alignItems: "baseline" } },
            h("span", { class: "badge muted" }, `#${i}`),
            h("span", { style: { color: "var(--accent-2)" } }, f.module || "?"),
            h("span", { class: "muted" }, f.function || "")),
          h("div", { class: "hint mono" }, `line ${f.line}`),
          f.source ? h("pre", { class: "log-detail", style: { margin: "5px 0 0", padding: "6px 9px" } }, f.source) : null))
      : [empty("No stack captured", "Send events with a stack array to see frames.")]), { pad: true })),
    h("div", { style: { flex: "1 1 280px", minWidth: "260px" } },
      panel("Tags", h("div", { class: "chips" }, Object.entries(issue.tags).map(([k, v]) => h("span", { class: "chip" }, `${k}: ${v}`))), { pad: true }),
      h("div", { class: "mt14" }, panel("Recent events", h("div", {}, issue.events.slice(0, 12).map((e) =>
        h("div", { class: "notif" },
          h("span", { class: "n-time" }, clock(e.ts, true)),
          h("div", { class: "flex1" },
            h("div", { class: "mono", style: { fontSize: "11px" } }, e.user_id || "anonymous"),
            h("div", { class: "hint" }, Object.entries(e.context || {}).slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(" · "))),
          e.trace_id ? h("a", { class: "link mono", href: `#/traces/${e.trace_id}`, style: { fontSize: "10px" } }, "trace") : null))), { pad: true })))));
}
