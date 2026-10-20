import { h, clear, panel, badge, levelBadge, empty, clock, toast, num, debounce, ago } from "../ui.js";
import { volumeBars } from "../charts.js";

const QUICK = [
  { label: "All logs", q: '{service=~".+"}' },
  { label: "Errors only", q: '{service=~".+"} |= "error"' },
  { label: "HTTP 5xx", q: '{service=~".+"} |~ "status=5[0-9][0-9]"' },
  { label: "Timeouts", q: '{service=~".+"} |~ "(?i)timeout|deadline"' },
  { label: "Checkout", q: '{service="checkout-service"}' },
  { label: "Payments errors", q: '{service="payment-service"} |~ "(?i)error|declin|deadlock"' },
];

export async function render(root, ctx) {
  ctx.setTitle("Logs");
  const params = ctx.route.params;
  let query = params.get("query") || localStorage.getItem("craft.lastLogQuery") || QUICK[0].q;
  let limit = 200;
  let levels = new Set();
  let live = false;
  let liveTimer = null;
  let data = { entries: [], count: 0 };
  let range = ctx.range;

  const qInput = h("input", { class: "input mono q-input", value: query, spellcheck: "false", placeholder: '{service="api-gateway"} |= "error"' });
  const runBtn = h("button", { class: "btn primary" }, "Run");
  const liveBtn = h("button", { class: "btn" }, "Live tail");
  const volumeHost = h("div", { class: "panel", style: { marginBottom: "10px", overflow: "hidden" } });
  const listHost = h("div", { class: "panel log-list", style: { maxHeight: "58vh", overflow: "auto" } });
  const statsHost = h("div", {});
  const summary = h("div", { class: "row", style: { alignItems: "center", gap: "10px", marginBottom: "8px" } });

  const levelChips = h("div", { class: "level-filter" }, ["error", "warn", "info", "debug"].map((lvl) =>
    h("button", { class: "chip add", onClick: (e) => {
      if (levels.has(lvl)) levels.delete(lvl); else levels.add(lvl);
      e.target.classList.toggle("active");
      e.target.style.background = levels.has(lvl) ? "rgba(154,230,110,0.18)" : "";
      e.target.style.color = levels.has(lvl) ? "var(--accent)" : "";
      drawList();
    } }, lvl)));

  async function fetchAll() {
    query = qInput.value.trim() || QUICK[0].q;
    localStorage.setItem("craft.lastLogQuery", query);
    range = ctx.range;
    try {
      const [entries, volume, stats] = await Promise.all([
        ctx.api.logs(query, range.start, range.end, 2000),
        ctx.api.logVolume(`${query}`, range.start, range.end, Math.max(60, Math.round((range.end - range.start) / 60))),
        ctx.api.logStats(query, range.start, range.end),
      ]);
      data = entries;
      drawVolume(volume);
      drawStats(stats);
      drawList();
    } catch (err) {
      clear(listHost);
      listHost.appendChild(empty("Query failed", err.message));
    }
  }

  function drawVolume(vol) {
    clear(volumeHost);
    const values = [];
    for (const s of vol.series || []) for (const [ts, v] of s.values) values.push({ ts: ts * 1000, value: v });
    const buckets = new Map();
    for (const v of values) buckets.set(v.ts, (buckets.get(v.ts) || 0) + v.value);
    const arr = [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([ts, value]) => ({ ts, value }));
    const el = h("div", { style: { padding: "8px 8px 0" } });
    volumeHost.appendChild(el);
    volumeBars(el, arr, {
      height: 72, label: "lines",
      colorFor: (b) => (b.value > 20 ? "#c96a5a" : "#4b8f5f"),
      onSelect: (b) => {
        toast(`Window ${clock(b.ts / 1000)} · ${b.value} lines`);
      },
    });
    volumeHost.appendChild(h("div", { class: "row", style: { padding: "2px 10px 7px", fontSize: "10.5px", color: "var(--text-3)" } },
      h("span", {}, `${num(values.reduce((a, b) => a + b.value, 0))} lines in range`),
      h("span", { class: "spacer" }),
      h("span", {}, "click a bar to inspect")));
  }

  function drawStats(stats) {
    clear(statsHost);
    statsHost.appendChild(h("div", { class: "panel panel-pad" },
      h("div", { class: "row", style: { gap: "18px", flexWrap: "wrap" } },
        h("div", {}, h("div", { class: "k-label", style: { fontSize: "10px", color: "var(--text-3)", textTransform: "uppercase" } }, "Matched"), h("div", { class: "num", style: { fontSize: "18px" } }, num(stats.total))),
        ...Object.entries(stats.by_level).slice(0, 4).map(([lvl, n]) => h("div", {}, h("div", { class: "k-label", style: { fontSize: "10px", color: "var(--text-3)", textTransform: "uppercase" } }, lvl), h("div", { class: "num", style: { fontSize: "18px", color: lvl === "error" ? "var(--red)" : lvl === "warn" ? "var(--yellow)" : "inherit" } }, num(n)))),
        h("div", { class: "spacer" }),
        h("div", { style: { minWidth: "180px" } },
          h("div", { class: "k-label", style: { fontSize: "10px", color: "var(--text-3)", textTransform: "uppercase", marginBottom: "5px" } }, "Top services"),
          h("div", { class: "chips" }, Object.entries(stats.by_service).slice(0, 5).map(([svc, n]) =>
            h("button", { class: "chip add", onClick: () => { qInput.value = `{service="${svc}"}`; fetchAll(); } }, `${svc} ${n}`)))))));
  }

  function drawList() {
    clear(listHost);
    let entries = data.entries;
    if (levels.size) entries = entries.filter((e) => levels.has((e.level || "info").toLowerCase()));
    clear(summary);
    summary.appendChild(h("span", { class: "badge ok" }, `${entries.length} lines`));
    summary.appendChild(h("span", { class: "hint" }, `${ctx.range.long} · newest first`));
    summary.appendChild(h("div", { class: "spacer" }));
    summary.appendChild(h("span", { class: "hint" }, "level"));
    summary.appendChild(levelChips);
    if (!entries.length) { listHost.appendChild(empty("No matching lines", "Broaden the stream selector or widen the time range.")); return; }
    for (const e of entries) {
      const row = h("div", { class: `log-row ${e.level === "error" ? "err" : e.level === "warn" ? "warn" : ""}`, style: { cursor: "pointer" } });
      const body = h("span", { class: "l-body" }, highlight(e.body, query));
      row.append(
        h("span", { class: "l-time" }, clock(e.ts, true)),
        h("span", { class: "l-level", style: { color: e.level === "error" ? "var(--red)" : e.level === "warn" ? "var(--yellow)" : "var(--text-3)" } }, (e.level || "info").slice(0, 5)),
        h("span", { class: "l-svc" }, e.service || "—"),
        body
      );
      let open = false;
      let detail = null;
      row.addEventListener("click", () => {
        if (open) { detail?.remove(); detail = null; open = false; return; }
        open = true;
        const attrs = { ...e.labels, ...e.attrs };
        detail = h("div", { class: "log-detail" },
          h("pre", {}, e.body),
          h("div", { class: "kv" }, Object.entries(attrs).filter(([k]) => k !== "service_name").map(([k, v]) => h("span", { class: "kv-chip" }, `${k}=${v}`))),
          e.trace_id ? h("div", { class: "mt8" }, h("a", { class: "link mono", href: `#/traces/${e.trace_id}` }, `trace ${e.trace_id.slice(0, 16)}…`)) : null
        );
        row.after(detail);
      });
      listHost.appendChild(row);
    }
  }

  const highlight = (body) => {
    const span = h("span", {}, body);
    const m = query.match(/\|=\s*"([^"]+)"/);
    if (m) {
      const term = m[1];
      const idx = body.toLowerCase().indexOf(term.toLowerCase());
      if (idx >= 0) {
        span.textContent = "";
        span.append(body.slice(0, idx), h("mark", { style: { background: "rgba(245,196,81,0.25)", color: "#ffe8b0", borderRadius: "2px" } }, body.slice(idx, idx + term.length)), body.slice(idx + term.length));
      }
    }
    return span;
  };

  qInput.addEventListener("keydown", (e) => { if (e.key === "Enter") fetchAll(); });
  runBtn.addEventListener("click", fetchAll);
  liveBtn.addEventListener("click", () => {
    live = !live;
    liveBtn.classList.toggle("primary", live);
    liveBtn.textContent = live ? "Live ●" : "Live tail";
    clearInterval(liveTimer);
    if (live) {
      liveTimer = setInterval(() => { if (!document.hidden) fetchAll(); }, 5000);
      toast("Live tail on — refreshing every 5s");
    }
  });
  qInput.addEventListener("input", debounce(() => {
    const q = qInput.value.trim();
    if (q.startsWith("{") && (q.endsWith("}") || q.includes("|"))) fetchAll();
  }, 550));

  root.appendChild(h("div", { class: "page-head" },
    h("div", { class: "grow" }, h("h1", {}, "Logs"), h("p", { class: "sub" }, "LogQL-style stream selectors, line filters and regex. Data lands here via Loki push, OTLP or JSON.")),
    h("div", { class: "row", style: { gap: "6px" } },
      h("button", { class: "btn", onClick: () => { ctx.go("/collector"); } }, "Send logs"),
      h("a", { class: "btn", href: "#/explore" }, "Docs"))));

  root.appendChild(h("div", { class: "panel panel-pad", style: { marginBottom: "10px" } },
    h("div", { class: "query-bar" }, qInput, runBtn, liveBtn,
      h("select", { class: "select", style: { width: "92px" }, onChange: (e) => { limit = Number(e.target.value); fetchAll(); } },
        [100, 200, 500, 1000].map((n) => h("option", { value: n, selected: n === limit }, `${n} lines`)))),
    h("div", { class: "chips mt8" }, QUICK.map((q) => h("button", { class: "chip add", onClick: () => { qInput.value = q.q; fetchAll(); } }, q.label)))));

  root.appendChild(volumeHost);
  root.appendChild(summary);
  root.appendChild(listHost);
  root.appendChild(h("div", { class: "mt14" }, statsHost));

  await fetchAll();
  ctx.setCleanup(() => clearInterval(liveTimer));
}
