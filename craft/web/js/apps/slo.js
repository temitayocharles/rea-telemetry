import { h, clear, panel, badge, empty, num, byUnit, toast, modal, formField, confirmDialog } from "../ui.js";
import { sparkline, mount, timeSeries } from "../charts.js";

export async function render(root, ctx) {
  ctx.setTitle("SLOs");
  const { slos } = await ctx.api.slos();
  const r = ctx.range;

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Service level objectives"), h("p", { class: "sub" }, "Objectives computed from the same series. Error budgets, burn rates and projected exhaustion.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("a", { class: "btn", href: "#/dashboards/slo-error-budgets" }, "Dashboard →"),
      h("button", { class: "btn primary", onClick: () => openSlo(ctx, null) }, "New SLO"))));

  if (!slos.length) { root.appendChild(empty("No SLOs defined", "Start with availability and latency objectives for your tier-1 services.")); return; }

  const breached = slos.filter((s) => s.status === "breached").length;
  root.appendChild(h("div", { class: "kpi-grid mb14" },
    h("div", { class: `kpi ${breached ? "bad" : ""}` }, h("div", { class: "k-label" }, "Objectives"), h("div", { class: "k-value" }, slos.length), h("div", { class: "k-sub" }, `${breached} breached`)),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Mean availability"), h("div", { class: "k-value" }, meanAvailability(slos))),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Budget remaining"), h("div", { class: "k-value" }, meanBudget(slos))),
    h("div", { class: "kpi warn" }, h("div", { class: "k-label" }, "Fastest burn"), h("div", { class: "k-value" }, maxBurn(slos)))));

  const grid = h("div", { class: "slo-grid" });
  root.appendChild(grid);

  for (const s of slos) {
    const remaining = s.budget_remaining_pct ?? 0;
    const used = s.budget_used_pct ?? 0;
    const tone = remaining < 10 ? "crit" : remaining < 40 ? "warn" : "";
    const history = (s.history || []).map((h2) => h2.availability);
    const chartHost = h("div", {});
    grid.appendChild(h("div", { class: `panel slo-card`, style: { borderColor: s.status === "breached" ? "#4a2626" : undefined } },
      h("div", { class: "head" },
        h("div", { class: "flex1" }, h("h3", {}, s.name), h("div", { class: "hint", style: { marginTop: "2px" } }, `${s.service} · ${s.window_days}d window · objective ${(s.objective * 100).toFixed(2)}%`)),
        h("div", { class: "row", style: { gap: "4px" } },
          h("button", { class: "btn ghost sm", onClick: () => openSlo(ctx, s) }, "edit"),
          h("button", { class: "btn ghost sm", onClick: () => confirmDialog("Delete SLO?", s.name, async () => { await ctx.api.deleteSlo(s.id); toast("SLO deleted"); ctx.refresh(); }, "Delete", true) }, "×"))),
      s.error ? h("div", { class: "badge crit", style: { marginTop: "8px" } }, s.error) : null,
      h("div", { class: "big" }, s.availability !== null && s.availability !== undefined ? (s.availability * 100).toFixed(3) + "%" : "—",
        h("span", { style: { fontSize: "12px", color: "var(--text-3)", fontFamily: "var(--sans)", marginLeft: "8px" } }, "actual")),
      h("div", { class: "budget-bar" },
        h("i", { style: { width: Math.min(100, remaining) + "%", background: tone === "crit" ? "var(--red)" : tone === "warn" ? "var(--yellow)" : "var(--accent)" } }),
        h("i", { style: { width: Math.min(100, used) + "%", background: "#2a2f38" } })),
      h("div", { class: "row", style: { fontSize: "11px", color: "var(--text-3)" } },
        h("span", {}, `${remaining.toFixed(1)}% budget left`),
        h("span", { class: "spacer" }),
        h("span", {}, `${num(s.error_budget_minutes || 0)} of ${num(s.allowed_bad_minutes || 0)} min burned`)),
      h("div", { class: "burn" },
        h("div", {}, h("div", { class: "b-label" }, "Burn 1h"), h("div", { class: `b-val ${burnTone(s.burn_rate_1h)}` }, (s.burn_rate_1h || 0).toFixed(1) + "×")),
        h("div", {}, h("div", { class: "b-label" }, "Burn 6h"), h("div", { class: `b-val ${burnTone(s.burn_rate_6h)}` }, (s.burn_rate_6h || 0).toFixed(1) + "×")),
        h("div", {}, h("div", { class: "b-label" }, "Burn 24h"), h("div", { class: `b-val ${burnTone(s.burn_rate_24h)}` }, (s.burn_rate_24h || 0).toFixed(1) + "×"))),
      h("div", { class: "mt14", style: { height: "44px" } }, chartHost),
      h("div", { class: "hint", style: { marginTop: "4px" } }, s.description || "")));
    mount(chartHost, () => {
      const el = h("div", {});
      chartHost.appendChild(el);
      el.appendChild(sparkline(history, { width: chartHost.clientWidth || 280, height: 40, color: s.status === "breached" ? "#ff6b6b" : "#9ae66e", fill: true }));
    });
  }

  // burn chart across SLOs
  const burnHost = h("div", {});
  root.appendChild(h("div", { class: "mt14" }, panel("Burn rate comparison (1h)", burnHost, { pad: true })));
  mount(burnHost, () => {
    const width = burnHost.clientWidth || 700;
    const svgNS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${Math.max(90, slos.length * 26)}`);
    svg.setAttribute("style", "width:100%");
    const max = Math.max(...slos.map((s) => s.burn_rate_1h || 0), 1);
    slos.forEach((s, i) => {
      const w = ((s.burn_rate_1h || 0) / max) * (width - 240);
      const y = i * 26;
      const label = document.createElementNS(svgNS, "text");
      label.setAttribute("x", 0); label.setAttribute("y", y + 14);
      label.setAttribute("style", "font: 10.5px var(--mono); fill: var(--text-2)");
      label.textContent = s.name.length > 26 ? s.name.slice(0, 25) + "…" : s.name;
      svg.appendChild(label);
      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", 200); rect.setAttribute("y", y + 3); rect.setAttribute("height", 12); rect.setAttribute("rx", 3);
      rect.setAttribute("width", Math.max(1, w));
      rect.setAttribute("fill", (s.burn_rate_1h || 0) > 2 ? "#a03d3d" : (s.burn_rate_1h || 0) > 1 ? "#8c6f22" : "#3d7a4d");
      svg.appendChild(rect);
      const val = document.createElementNS(svgNS, "text");
      val.setAttribute("x", 200 + Math.max(1, w) + 6); val.setAttribute("y", y + 14);
      val.setAttribute("style", "font: 10.5px var(--mono); fill: var(--text-3)");
      val.textContent = (s.burn_rate_1h || 0).toFixed(2) + "×";
      svg.appendChild(val);
    });
    burnHost.appendChild(svg);
  });

  root.appendChild(h("div", { class: "mt14" }, panel("How these are computed", h("div", { class: "stack sub" },
    h("p", { style: { margin: 0 } }, "Each SLO pairs a good-events series with a total-events series. Availability is Σ good / Σ total across the window, sampled every step."),
    h("p", { style: { margin: 0 } }, "Burn rate is the current bad-event ratio divided by the error budget. 1× means you will exactly exhaust the budget at window end; >2× is a page-worthy burn."),
    h("p", { style: { margin: 0 } }, "Budget remaining = 1 − (bad events observed / bad events allowed). Multiplication of multi-window burns follows the Google SRE workbook pattern.")), { pad: true })));
}

const meanAvailability = (slos) => {
  const vals = slos.map((s) => s.availability).filter((v) => v !== null && v !== undefined);
  return vals.length ? ((vals.reduce((a, b) => a + b, 0) / vals.length) * 100).toFixed(3) + "%" : "—";
};
const meanBudget = (slos) => {
  const vals = slos.map((s) => s.budget_remaining_pct).filter((v) => v !== null && v !== undefined);
  return vals.length ? (vals.reduce((a, b) => a + b, 0) / vals.length).toFixed(1) + "%" : "—";
};
const maxBurn = (slos) => Math.max(...slos.map((s) => s.burn_rate_1h || 0), 0).toFixed(1) + "×";
const burnTone = (v) => ((v || 0) > 2 ? "crit" : (v || 0) > 1 ? "warn" : "");

function openSlo(ctx, slo) {
  const nameIn = h("input", { class: "input", value: slo?.name || "" });
  const serviceIn = h("input", { class: "input", value: slo?.service || "" });
  const objIn = h("input", { class: "input", type: "number", step: "0.001", value: slo?.objective ?? 0.999 });
  const windowIn = h("select", { class: "select" }, [7, 14, 30, 90].map((d) => h("option", { value: d, selected: (slo?.window_days || 30) === d }, `${d} days`)));
  const goodIn = h("textarea", { class: "textarea", value: slo?.sli_expr || 'sum(rate(http_requests_total{service="checkout-service",status!~"5.."}[5m]))' });
  const totalIn = h("textarea", { class: "textarea", value: slo?.total_expr || 'sum(rate(http_requests_total{service="checkout-service"}[5m]))' });
  const descIn = h("input", { class: "input", value: slo?.description || "" });
  const m = modal({
    title: slo ? "Edit SLO" : "New SLO",
    wide: true,
    body: [
      h("div", { class: "form-grid" }, formField("Name", nameIn), formField("Service", serviceIn), formField("Objective", objIn, "0.999 = 99.9%"), formField("Window", windowIn)),
      formField("Good events (numerator)", goodIn, "A rate/ratio expression; the collector sums it across the window."),
      formField("Total events (denominator)", totalIn),
      formField("Description", descIn),
    ],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        try {
          await ctx.api.saveSlo({ name: nameIn.value, service: serviceIn.value, objective: Number(objIn.value), window_days: Number(windowIn.value), sli_expr: goodIn.value, total_expr: totalIn.value, description: descIn.value });
          toast("SLO saved");
          m.close();
          ctx.refresh();
        } catch (err) { toast(err.message, "err"); }
      } }, "Save"),
    ],
  });
}
