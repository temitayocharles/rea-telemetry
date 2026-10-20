import { h, clear, panel, badge, empty, num, byUnit, toast, ago, clock } from "../ui.js";
import { timeSeries, mount, barList } from "../charts.js";

export async function render(root, ctx) {
  ctx.setTitle("Load tests");
  let data = await ctx.api.loadtests();
  let watchTimer = null;

  const nameIn = h("input", { class: "input", value: "checkout smoke", placeholder: "test name" });
  const targetIn = h("input", { class: "input mono", value: "/api/status/echo?path=/v1/checkout", placeholder: "https://your-service/health" });
  const vusIn = h("input", { class: "input", type: "number", value: 5, min: 1, max: 20 });
  const durIn = h("input", { class: "input", type: "number", value: 20, min: 1, max: 60 });
  const rampIn = h("input", { type: "checkbox", checked: "checked" });
  const listHost = h("div", {});

  const run = async () => {
    const target = targetIn.value.trim();
    if (!target) return toast("Enter a target URL", "err");
    const abs = target.startsWith("http") ? target : location.origin + target;
    const res = await ctx.api.startLoadtest({ name: nameIn.value, target: abs, vus: Number(vusIn.value), duration_s: Number(durIn.value), ramp: rampIn.checked });
    toast(`Load test #${res.id} started — ${res.vus} VUs for ${res.duration_s}s`);
    clearInterval(watchTimer);
    watchTimer = setInterval(async () => {
      data = await ctx.api.loadtests();
      draw();
      const running = data.tests.some((t) => t.status === "running");
      if (!running) { clearInterval(watchTimer); toast("Load test finished"); }
    }, 2000);
  };

  const draw = () => {
    clear(listHost);
    if (!data.tests.length) { listHost.appendChild(empty("No load tests yet", "Run a bounded test to see latency and error curves.")); return; }
    for (const t of data.tests) {
      const chartHost = h("div", {});
      const summary = t.summary || {};
      const cfg = t.config || {};
      listHost.appendChild(panel(`${t.name} ${t.status === "running" ? "" : ""}`, h("div", {},
        h("div", { class: "row", style: { gap: "18px", flexWrap: "wrap", alignItems: "center", marginBottom: "8px" } },
          h("span", { class: `badge ${t.status === "running" ? "warn" : "ok"}` }, t.status),
          h("span", { class: "hint mono" }, `${cfg.vus || "?"} VUs · ${cfg.duration_s || "?"}s · ramp ${cfg.ramp ? "on" : "off"}`),
          h("span", { class: "hint mono truncate", style: { maxWidth: "360px" } }, cfg.target || ""),
          h("span", { class: "spacer" }),
          t.status === "running" ? h("button", { class: "btn sm danger", onClick: async () => { await ctx.api.cancelLoadtest(t.id); toast("Cancelling"); } }, "Stop") : null),
        h("div", { class: "kpi-grid", style: { gridTemplateColumns: "repeat(auto-fit,minmax(110px,1fr))", marginBottom: "8px" } },
          stat("Requests", num(summary.requests || 0)),
          stat("RPS", num(summary.rps || 0)),
          stat("p50", byUnit(summary.p50 || 0, "ms")),
          stat("p95", byUnit(summary.p95 || 0, "ms")),
          stat("p99", byUnit(summary.p99 || 0, "ms")),
          stat("Errors", (summary.error_rate || 0) + "%", (summary.error_rate || 0) > 1 ? "var(--red)" : "")),
        chartHost,
        h("div", { class: "hint", style: { marginTop: "6px" } }, t.started_at ? `started ${ago(t.started_at)} · ${t.finished_at ? "finished in " + (t.finished_at - t.started_at) + "s" : "running"}` : "")), {
        pad: true,
        meta: `#${t.id}`,
        actions: h("span", { class: "badge muted" }, `${(t.points || []).length} samples`),
      }));
      if (t.points?.length) {
        mount(chartHost, () => {
          const el = h("div", {});
          chartHost.appendChild(el);
          timeSeries(el, [
            { label: "p50", color: "#62a8ff", values: t.points.map((p) => [p.ts, p.p50]) },
            { label: "p95", color: "#9ae66e", values: t.points.map((p) => [p.ts, p.p95]) },
            { label: "p99", color: "#f5c451", values: t.points.map((p) => [p.ts, p.p99]) },
          ], { height: 150, unit: "ms", legend: true });
        });
      }
    }
  };

  const stat = (label, value, color) => h("div", { class: "kpi", style: { padding: "8px 10px" } },
    h("div", { class: "k-label" }, label), h("div", { class: "k-value", style: { fontSize: "17px", color: color || "inherit" } }, value));

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Load tests"), h("p", { class: "sub" }, "A k6-style generator built in. Real HTTP traffic against your own target, bounded to 20 VUs and 60s so nothing runs away."))));

  root.appendChild(h("div", { class: "row", style: { alignItems: "flex-start", gap: "12px", flexWrap: "wrap", marginBottom: "14px" } },
    h("div", { style: { flex: "1 1 520px", minWidth: "300px" } }, panel("New run", h("div", {},
      h("div", { class: "form-grid" },
        field("Name", nameIn), field("Virtual users", vusIn), field("Duration (s)", durIn)),
      h("div", { class: "mt8" }, field("Target URL", targetIn, "Relative paths resolve to this server.")),
      h("div", { class: "row mt8", style: { alignItems: "center" } },
        h("label", { class: "row", style: { alignItems: "center", gap: "6px", fontSize: "12px", color: "var(--text-2)" } }, rampIn, "ramp up"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn primary", onClick: run }, "Start test"))), { pad: true })),
    h("div", { style: { flex: "1 1 300px", minWidth: "260px" } }, panel("Safety rails", h("div", { class: "stack sub" },
      h("p", { style: { margin: 0 } }, "• Hard cap of 20 concurrent virtual users and 60 seconds per test."),
      h("p", { style: { margin: 0 } }, "• 3s request timeout; every failure is recorded, never retried."),
      h("p", { style: { margin: 0 } }, "• Results are stored per second so you can compare p50/p95/p99 and error rate."),
      h("p", { style: { margin: 0 } }, "• Point it at your staging or a local target. This is a load generator, not a DDoS tool.")), { pad: true }))));

  root.appendChild(h("div", { class: "section" }, h("div", { class: "section-head" }, h("h2", {}, "Runs")), listHost));
  draw();
  ctx.setCleanup(() => clearInterval(watchTimer));
}

const field = (label, input, hint) => h("div", { class: "field" }, h("label", {}, label), input, hint ? h("div", { class: "hint" }, hint) : null);
