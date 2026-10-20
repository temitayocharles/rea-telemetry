import { h, clear, panel, badge, empty, bytes, num, toast, copy, copyButton, ago, clock, modal, formField } from "../ui.js";

export async function render(root, ctx) {
  ctx.setTitle("Collector");
  const [stats, pipelinesRes, settings] = await Promise.all([ctx.api.stats(), ctx.api.pipelines(), ctx.api.settings()]);
  const pipelines = pipelinesRes.pipelines;
  const endpoints = settings.ingest_endpoints;

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Collector"), h("p", { class: "sub" }, "One ingest surface for every signal, plus the pipeline config that describes it.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("button", { class: "btn", onClick: async () => { const res = await ctx.api.prune(); toast(`Pruned ${num(res.samples)} samples, ${num(res.logs)} logs`); ctx.refresh(); } }, "Prune now"),
      h("button", { class: "btn primary", onClick: () => openPipeline(ctx, null) }, "New pipeline"))));

  root.appendChild(h("div", { class: "kpi-grid mb14" },
    h("div", { class: "kpi accent" }, h("div", { class: "k-label" }, "Time series"), h("div", { class: "k-value" }, num(stats.series)), h("div", { class: "k-sub" }, `${num(stats.samples)} samples`)),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Samples / hour"), h("div", { class: "k-value" }, num(stats.samples_1h)), h("div", { class: "k-sub" }, "current ingest rate")),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Log lines"), h("div", { class: "k-value" }, num(stats.logs)), h("div", { class: "k-sub" }, `${num(stats.logs_1h)} in the last hour`)),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "Spans"), h("div", { class: "k-value" }, num(stats.spans)), h("div", { class: "k-sub" }, `${num(stats.span_errors)} errored`)),
    h("div", { class: "kpi" }, h("div", { class: "k-label" }, "On disk"), h("div", { class: "k-value" }, bytes(stats.db_bytes)), h("div", { class: "k-sub" }, `retention ${stats.retention_days}d`))));

  // ingest endpoints
  const endpointRows = endpoints.map((e) => h("tr", {},
    h("td", {}, h("div", { style: { fontWeight: 550 } }, e.name), h("div", { class: "hint mono" }, e.format)),
    h("td", { class: "mono" }, e.url),
    h("td", { class: "right" }, h("button", { class: "btn sm", onClick: () => copy(`${location.origin}${e.url}`) }, "Copy"))));
  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Ingest endpoints"), h("span", { class: "hint" }, "point existing agents here")),
    panel("", h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["Protocol", "Path", ""].map((c) => h("th", {}, c)))), h("tbody", {}, endpointRows)), { pad: false })));

  // quick start snippets
  const base = location.origin;
  const snippets = [
    ["Prometheus remote-write", `curl -X POST ${base}/api/prom/api/v1/write \\
  -H 'Content-Type: application/json' \\
  -d '{"timeseries":[{"metric":{"__name__":"my_gauge","job":"demo"},"values":[[${Math.floor(Date.now() / 1000)},"42"]]}]}'`],
    ["Simple JSON metrics", `curl -X POST ${base}/api/metrics/write \\
  -H 'Content-Type: application/json' \\
  -d '{"series":[{"metric":"queue_depth","labels":{"queue":"email"},"value":73}]}'`],
    ["Loki push", `curl -X POST ${base}/api/loki/api/v1/push -H 'Content-Type: application/json' \\
  -d '{"streams":[{"stream":{"service":"api"},"values":[["${Date.now()}000000000","level=info msg=hello"]]}]}'`],
    ["OTLP traces (JSON)", `curl -X POST ${base}/api/otlp/v1/traces -H 'Content-Type: application/json' \\
  -d '{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"api"}}]},"scopeSpans":[{"spans":[{"traceId":"abc123","spanId":"s1","name":"GET /","kind":"SPAN_KIND_SERVER","startTimeUnixNano":"${Date.now()}000000","endTimeUnixNano":"${Date.now() + 20}000000"}]}]}]}'`],
    ["Error event", `curl -X POST ${base}/api/events -H 'Content-Type: application/json' \\
  -d '{"title":"TypeError","service":"web","level":"error","message":"x is undefined","user_id":"u_1","tags":{"release":"v1.2.3"}}'`],
    ["Scrape (Prometheus text)", `curl ${base}/metrics`],
  ];
  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Send data in 10 seconds")),
    h("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit,minmax(340px,1fr))" } }, snippets.map(([title, code]) =>
      h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", {}, title), h("span", { class: "spacer" }), copyButton(code)),
        h("div", { style: { padding: "10px" } }, h("div", { class: "code-block" }, code)))))));

  // pipelines
  const pipeHost = h("div", {});
  const drawPipes = () => {
    clear(pipeHost);
    for (const p of pipelines) {
      pipeHost.appendChild(h("div", { class: "pipeline" },
        h("div", {}, h("div", { style: { fontWeight: 550 } }, p.name), h("div", { class: "hint" }, p.signal)),
        h("div", { class: "row", style: { alignItems: "center", flexWrap: "wrap", gap: "6px" } },
          h("div", { class: "chips" }, p.receivers.map((r) => h("span", { class: "chip" }, "in: " + r)), h("span", { class: "muted" }, "→"),
            p.processors.map((r) => h("span", { class: "chip" }, r)), h("span", { class: "muted" }, "→"),
            p.exporters.map((r) => h("span", { class: "chip" }, "out: " + r))),
          h("span", { class: "spacer" }),
          p.enabled ? badge("active", "ok") : badge("paused", "muted"),
          h("button", { class: "btn sm ghost", onClick: () => openPipeline(ctx, p) }, "edit"))));
    }
  };
  drawPipes();
  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Pipelines"), h("span", { class: "hint" }, "receivers → processors → exporters, as data")),
    panel("", h("div", { style: { padding: "4px 14px" } }, pipeHost), { pad: false })));

  root.appendChild(h("div", { class: "section" },
    h("div", { class: "section-head" }, h("h2", {}, "Storage engine")),
    panel("", h("div", { style: { padding: "14px" } }, h("div", { class: "row", style: { gap: "26px", flexWrap: "wrap" } },
      kv("Engine", "SQLite (WAL)"),
      kv("Database", stats.db_path),
      kv("Samples", num(stats.samples)),
      kv("Series", num(stats.series)),
      kv("Retention", stats.retention_days + " days"),
      kv("Size", bytes(stats.db_bytes)))), { pad: false })));
}

const kv = (k, v) => h("div", {}, h("div", { class: "k-label", style: { fontSize: "10px", textTransform: "uppercase", color: "var(--text-3)" } }, k), h("div", { class: "mono", style: { marginTop: "3px", fontSize: "11.5px", maxWidth: "420px", overflowWrap: "anywhere" } }, v));

function openPipeline(ctx, p) {
  const nameIn = h("input", { class: "input", value: p?.name || "" });
  const signalIn = h("select", { class: "select" }, ["metrics", "logs", "traces", "profiles"].map((s) => h("option", { value: s, selected: p?.signal === s }, s)));
  const recvIn = h("input", { class: "input mono", value: (p?.receivers || ["otlp"]).join(", ") });
  const procIn = h("input", { class: "input mono", value: (p?.processors || ["batch"]).join(", ") });
  const expIn = h("input", { class: "input mono", value: (p?.exporters || ["telemetrycraft"]).join(", ") });
  const m = modal({
    title: p ? "Edit pipeline" : "New pipeline",
    body: [
      h("div", { class: "form-grid" }, formField("Name", nameIn), formField("Signal", signalIn)),
      formField("Receivers", recvIn, "Comma separated"),
      formField("Processors", procIn),
      formField("Exporters", expIn),
    ],
    footer: [
      h("button", { class: "btn ghost", onClick: () => m.close() }, "Cancel"),
      h("button", { class: "btn primary", onClick: async () => {
        await ctx.api.savePipeline({ name: nameIn.value, signal: signalIn.value, receivers: split(recvIn.value), processors: split(procIn.value), exporters: split(expIn.value), enabled: true });
        toast("Pipeline saved");
        m.close();
        ctx.refresh();
      } }, "Save"),
    ],
  });
  const split = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
}
