// Hand-rolled SVG charts: time series, sparklines, volume bars, gauges, heatmaps, scatter.
import { h, byUnit, clock, colorFor } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs = {}) => {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  return el;
};

let tooltipEl = null;
function tooltip() {
  if (!tooltipEl) {
    tooltipEl = h("div", { class: "chart-tooltip", style: { display: "none" } });
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}
function hideTooltip() { tooltip().style.display = "none"; }

const toNum = (v) => {
  const n = typeof v === "number" ? v : parseFloat(v);
  return Number.isFinite(n) ? n : null;
};

function niceTicks(min, max, count = 4) {
  if (min === max) { max = min + 1; }
  const span = max - min;
  const raw = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

function timeTicks(start, end, width) {
  const span = end - start;
  const targets = Math.max(3, Math.min(7, Math.floor(width / 110)));
  const steps = [30e3, 60e3, 5 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3, 86400e3, 2 * 86400e3, 7 * 86400e3];
  const step = steps.find((s) => span / s <= targets) || steps[steps.length - 1];
  const offset = new Date(start).getTimezoneOffset() * 60e3;
  const first = Math.ceil((start - offset) / step) * step + offset;
  const ticks = [];
  for (let t = first; t <= end; t += step) ticks.push(t);
  return { ticks, step };
}

const xLabelFmt = (ms, step) => {
  const d = new Date(ms);
  if (step >= 86400e3) return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  if (step >= 3600e3) return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: step < 60e3 ? "2-digit" : undefined, hour12: false });
};

export function mount(el, draw) {
  let frame = null;
  const render = () => {
    if (!el.isConnected) return;
    const prev = el.querySelector("svg");
    const w = el.clientWidth || 640;
    if (prev && Number(prev.dataset.w) === w && el.dataset.dirty !== "1") return;
    draw(w);
  };
  const schedule = () => {
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(render);
  };
  const ro = new ResizeObserver(schedule);
  ro.observe(el);
  schedule();
  return { redraw: () => { el.dataset.dirty = "1"; schedule(); }, stop: () => ro.disconnect() };
}

/**
 * series: [{ label, color, values: [[tsSeconds, value]] }]
 * opts: { height, unit, start, end, stacked, area, min, max, legend, zero, thresholds }
 */
export function timeSeries(el, series, opts = {}) {
  const height = opts.height || 200;
  const unit = opts.unit || "short";
  const pad = { top: 10, right: 12, bottom: 20, left: 46 };
  const width = Math.max(el.clientWidth || 640, 260);
  const data = (series || []).filter((s) => s && s.values && s.values.length);
  el.innerHTML = "";
  if (!data.length) {
    el.appendChild(h("div", { class: "empty", style: { padding: "26px 0" } }, "No data for this range"));
    return;
  }
  let start = opts.start, end = opts.end;
  if (!start || !end) {
    const ts = data.flatMap((s) => s.values.map((v) => Number(v[0]) * 1000));
    start = Math.min(...ts); end = Math.max(...ts);
  }
  if (start === end) { start -= 60000; end += 60000; }
  const values = data.flatMap((s) => s.values.map((v) => toNum(v[1])).filter((v) => v !== null));
  let min = opts.min !== undefined ? opts.min : (opts.zero === false ? Math.min(...values) : Math.min(0, Math.min(...values)));
  let max = opts.max !== undefined ? opts.max : Math.max(...values);
  if (!Number.isFinite(min)) min = 0;
  if (!Number.isFinite(max) || max === min) max = min + 1;
  if (opts.stacked) {
    const totals = new Map();
    for (const s of data) for (const [ts, v] of s.values) totals.set(ts, (totals.get(ts) || 0) + (toNum(v) || 0));
    max = Math.max(max, ...totals.values());
  }
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const x = (ms) => pad.left + ((ms - start) / (end - start)) * innerW;
  const y = (v) => pad.top + innerH - ((v - min) / (max - min)) * innerH;

  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, class: "chart" });
  const defs = svgEl("defs");
  svg.appendChild(defs);

  // grid + y axis
  for (const tick of niceTicks(min, max, Math.max(3, Math.floor(innerH / 42)))) {
    const yy = y(tick);
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: yy, y2: yy, class: "grid-line" }));
    const t = svgEl("text", { x: pad.left - 7, y: yy + 3.5, "text-anchor": "end" });
    t.textContent = byUnit(tick, unit);
    svg.appendChild(t);
  }
  // x axis
  const { ticks: xticks, step } = timeTicks(start, end, innerW);
  for (const t of xticks) {
    const xx = x(t);
    svg.appendChild(svgEl("line", { x1: xx, x2: xx, y1: pad.top, y2: pad.top + innerH, class: "grid-line", opacity: 0.55 }));
    const label = svgEl("text", { x: xx, y: height - 5, "text-anchor": "middle" });
    label.textContent = xLabelFmt(t, step);
    svg.appendChild(label);
  }

  // thresholds
  (opts.thresholds || []).forEach((th) => {
    if (th.value < min || th.value > max) return;
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: y(th.value), y2: y(th.value), stroke: th.color || "#8a5a5a", "stroke-width": 1, "stroke-dasharray": "3 3" }));
  });

  // series paths
  const baseline = new Map();
  data.forEach((s, i) => {
    const color = s.color || colorFor(i);
    const pts = s.values.map(([ts, v]) => [Number(ts) * 1000, toNum(v)]).filter(([ts, v]) => v !== null && ts >= start - 1 && ts <= end + 1);
    if (!pts.length) return;
    const stepX = pts.length > 1 ? (pts[1][0] - pts[0][0]) : 0;
    const path = [];
    const areaPath = [];
    let started = false;
    for (const [ts, v] of pts) {
      let val = v;
      if (opts.stacked) {
        const baseVal = baseline.get(ts) || 0;
        baseline.set(ts, baseVal + v);
        val = baseVal + v;
        const lower = baseVal;
        if (!started) { areaPath.push(`M ${x(ts)} ${y(lower)}`); }
        areaPath.push(`L ${x(ts)} ${y(val)}`);
        if (started) path.push(`L ${x(ts)} ${y(val)}`);
        else { path.push(`M ${x(ts)} ${y(val)}`); started = true; }
      } else {
        path.push(`${started ? "L" : "M"} ${x(ts)} ${y(v)}`);
        started = true;
      }
    }
    if (opts.stacked || opts.area) {
      const bb = pts[pts.length - 1];
      if (opts.stacked) areaPath.push(`L ${x(bb[0])} ${y(baseline.get(bb[0]) - (toNum(bb[1]) || 0))}`);
      else areaPath.push(`L ${x(bb[0])} ${y(Math.max(min, 0))}`, `L ${x(pts[0][0])} ${y(Math.max(min, 0))}`);
      areaPath.push("Z");
      defs.appendChild(svgEl("linearGradient", { id: `g${i}-${Math.random().toString(36).slice(2, 7)}` }));
      svg.appendChild(svgEl("path", { d: areaPath.join(" "), fill: color, opacity: opts.stacked ? 0.5 : 0.13, stroke: "none" }));
    }
    const p = svgEl("path", { d: path.join(" "), fill: "none", stroke: color, "stroke-width": opts.thin ? 1.3 : 1.8, "stroke-linejoin": "round", "stroke-linecap": "round" });
    if (stepX > 180e3 && !opts.line) {
      svg.appendChild(p);
    } else {
      svg.appendChild(p);
    }
  });

  // interaction layer
  const overlay = svgEl("rect", { x: pad.left, y: pad.top, width: innerW, height: innerH, fill: "transparent" });
  svg.appendChild(overlay);
  const cross = svgEl("line", { y1: pad.top, y2: pad.top + innerH, stroke: "#5b6673", "stroke-width": 1, "stroke-dasharray": "2 2", opacity: 0 });
  svg.appendChild(cross);

  const stamps = [...new Set(data.flatMap((s) => s.values.map((v) => Number(v[0]) * 1000)))].sort((a, b) => a - b);
  const onMove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const scale = width / rect.width;
    const mx = (ev.clientX - rect.left) * scale;
    if (mx < pad.left || mx > width - pad.right) { hideTooltip(); cross.setAttribute("opacity", 0); return; }
    const target = start + ((mx - pad.left) / innerW) * (end - start);
    let best = stamps[0];
    for (const t of stamps) if (Math.abs(t - target) < Math.abs(best - target)) best = t;
    const xx = x(best);
    cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.setAttribute("opacity", 1);
    const rows = data.map((s, i) => {
      const found = s.values.find((v) => Number(v[0]) * 1000 === best);
      if (!found) return null;
      const val = toNum(found[1]);
      return { label: s.label, color: s.color || colorFor(i), value: val === null ? "—" : byUnit(val, unit) };
    }).filter(Boolean);
    const tip = tooltip();
    tip.innerHTML = "";
    tip.appendChild(h("div", { class: "tt-time" }, new Date(best).toLocaleString("en-GB", { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })));
    for (const r of rows.slice(0, 12)) {
      tip.appendChild(h("div", { class: "tt-row" },
        h("span", {}, h("i", { class: "legend-swatch", style: { background: r.color } }), r.label || "value"),
        h("span", { class: "mono" }, r.value)));
    }
    tip.style.display = "block";
    const bx = Math.min(window.innerWidth - tip.offsetWidth - 12, ev.clientX + 14);
    const by = Math.max(8, ev.clientY - tip.offsetHeight - 12);
    tip.style.left = bx + "px"; tip.style.top = by + "px";
  };
  overlay.addEventListener("mousemove", onMove);
  overlay.addEventListener("mouseleave", () => { hideTooltip(); cross.setAttribute("opacity", 0); });
  if (opts.onClick) overlay.addEventListener("click", (ev) => {
    const rect = svg.getBoundingClientRect();
    const scale = width / rect.width;
    const target = start + (((ev.clientX - rect.left) * scale - pad.left) / innerW) * (end - start);
    opts.onClick(Math.round(target / 1000));
  });

  el.appendChild(svg);
  if (opts.legend !== false && data.length > 1) {
    el.appendChild(h("div", { class: "legend" }, data.slice(0, 14).map((s, i) =>
      h("div", { class: "legend-item" }, h("i", { class: "legend-swatch", style: { background: s.color || colorFor(i) } }), s.label || `series ${i + 1}`))));
  }
}

export function sparkline(values, opts = {}) {
  const w = opts.width || 120, hgt = opts.height || 30;
  const nums = values.map((v) => toNum(v)).filter((v) => v !== null);
  const svg = svgEl("svg", { viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, class: "chart" });
  if (!nums.length) return svg;
  const min = opts.min !== undefined ? opts.min : Math.min(...nums);
  const max = opts.max !== undefined ? opts.max : Math.max(...nums);
  const span = max - min || 1;
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const color = opts.color || "#9ae66e";
  const pts = values.map((v, i) => [i * step, hgt - 3 - (((toNum(v) ?? min) - min) / span) * (hgt - 6)]);
  svg.appendChild(svgEl("path", { d: pts.map(([x, y], i) => `${i ? "L" : "M"} ${x.toFixed(1)} ${y.toFixed(1)}`).join(" "), fill: "none", stroke: color, "stroke-width": opts.stroke || 1.4, "stroke-linejoin": "round" }));
  if (opts.fill) {
    svg.appendChild(svgEl("path", { d: `${pts.map(([x, y], i) => `${i ? "L" : "M"} ${x.toFixed(1)} ${y.toFixed(1)}`).join(" ")} L ${w} ${hgt} L 0 ${hgt} Z`, fill: color, opacity: 0.12 }));
  }
  if (opts.bars) {
    svg.innerHTML = "";
    const bw = Math.max(1, w / values.length - 1);
    values.forEach((v, i) => {
      const val = toNum(v) || 0;
      const bh = (val / (max || 1)) * (hgt - 4);
      svg.appendChild(svgEl("rect", { x: i * (bw + 1), y: hgt - bh - 2, width: bw, height: Math.max(bh, 0.6), rx: 1, fill: opts.barColor || color, opacity: 0.75 }));
    });
  }
  return svg;
}

/** Interactive volume histogram. buckets: [{ts, value, meta}] */
export function volumeBars(el, buckets, opts = {}) {
  const width = Math.max(el.clientWidth || 640, 240);
  const height = opts.height || 76;
  el.innerHTML = "";
  if (!buckets.length) { el.appendChild(h("div", { class: "empty", style: { padding: "16px" } }, "No volume")); return; }
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, style: "display:block" });
  const max = Math.max(...buckets.map((b) => b.value), 1);
  const bw = width / buckets.length;
  buckets.forEach((b, i) => {
    const bh = (b.value / max) * (height - 12);
    const rect = svgEl("rect", {
      x: i * bw + (bw > 4 ? 1 : 0), y: height - 6 - bh, width: Math.max(bw - (bw > 4 ? 2 : 0), 0.8), height: Math.max(bh, 1.5), rx: 1.5,
      fill: opts.colorFor ? opts.colorFor(b) : "#4b8f5f", opacity: b.value ? 0.9 : 0.25,
    });
    rect.addEventListener("mouseenter", () => {
      const tip = tooltip();
      tip.innerHTML = "";
      tip.appendChild(h("div", { class: "tt-time" }, new Date(b.ts).toLocaleString("en-GB", { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })));
      tip.appendChild(h("div", { class: "tt-row" }, h("span", {}, opts.label || "count"), h("span", { class: "mono" }, b.value)));
      tip.style.display = "block";
      tip.style.left = Math.min(window.innerWidth - 160, event.clientX + 12) + "px";
      tip.style.top = Math.max(8, event.clientY - 60) + "px";
    });
    rect.addEventListener("mouseleave", hideTooltip);
    if (opts.onSelect) rect.addEventListener("click", () => opts.onSelect(b));
    svg.appendChild(rect);
  });
  svg.appendChild(svgEl("line", { x1: 0, x2: width, y1: height - 5, y2: height - 5, stroke: "#242b34" }));
  svg.appendChild(svgEl("line", { x1: 0, x2: width, y1: height - 1, y2: height - 1, stroke: "#161b22" }));
  el.appendChild(svg);
}

export function gauge(value, opts = {}) {
  const size = opts.size || 128;
  const pct = Math.max(0, Math.min(1, value || 0));
  const color = opts.color || (pct > 0.9 ? "#ff6b6b" : pct > 0.75 ? "#f5c451" : "#9ae66e");
  const r = size / 2 - 10;
  const circ = Math.PI * r;
  const svg = svgEl("svg", { viewBox: `0 0 ${size} ${size / 1.55}`, width: "100%", height: size / 1.55 });
  svg.appendChild(svgEl("path", { d: `M 10 ${size / 2} A ${r} ${r} 0 0 1 ${size - 10} ${size / 2}`, fill: "none", stroke: "#232932", "stroke-width": 8, "stroke-linecap": "round" }));
  svg.appendChild(svgEl("path", {
    d: `M 10 ${size / 2} A ${r} ${r} 0 0 1 ${size - 10} ${size / 2}`, fill: "none", stroke: color, "stroke-width": 8, "stroke-linecap": "round",
    "stroke-dasharray": `${circ * pct} ${circ}`, style: "transition: stroke-dasharray 0.4s ease",
  }));
  const label = svgEl("text", { x: size / 2, y: size / 2 - 6, "text-anchor": "middle", style: "font: 600 17px var(--mono); fill: var(--text)" });
  label.textContent = opts.label || `${(pct * 100).toFixed(1)}%`;
  svg.appendChild(label);
  if (opts.sub) {
    const sub = svgEl("text", { x: size / 2, y: size / 2 + 10, "text-anchor": "middle", style: "font: 10px var(--mono); fill: var(--text-3)" });
    sub.textContent = opts.sub;
    svg.appendChild(sub);
  }
  return svg;
}

/** buckets: series of {le, values[[ts,v]]} → latency heatmap */
export function heatmap(el, buckets, opts = {}) {
  const width = Math.max(el.clientWidth || 640, 260);
  const height = opts.height || 180;
  const pad = { left: 40, right: 8, top: 8, bottom: 18 };
  el.innerHTML = "";
  if (!buckets.length) { el.appendChild(h("div", { class: "empty" }, "No histogram data")); return; }
  const les = [...new Set(buckets.map((b) => Number(b.le)))].sort((a, b) => a - b);
  const stamps = [...new Set(buckets[0].values.map((v) => Number(v[0])))].sort((a, b) => a - b);
  const start = stamps[0] * 1000, end = stamps[stamps.length - 1] * 1000;
  const innerW = width - pad.left - pad.right, innerH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, class: "chart" });
  const lookup = new Map();
  for (const b of buckets) lookup.set(Number(b.le), new Map(b.values.map((v) => [Number(v[0]), toNum(v[1])])));
  // derive per-bucket counts and normalise
  let maxCount = 0;
  const cells = [];
  for (let i = 0; i < stamps.length - 1; i++) {
    let prev = 0;
    for (const le of les) {
      const cur = lookup.get(le)?.get(stamps[i + 1]) ?? 0;
      const count = Math.max(0, cur - prev);
      prev = cur;
      cells.push({ i, le, count });
      maxCount = Math.max(maxCount, count);
    }
  }
  const colW = innerW / Math.max(stamps.length - 1, 1);
  const rowH = innerH / les.length;
  for (const cell of cells) {
    const intensity = maxCount ? Math.pow(cell.count / maxCount, 0.42) : 0;
    const color = `hsl(${150 - intensity * 150}, ${35 + intensity * 45}%, ${8 + intensity * 44}%)`;
    svg.appendChild(svgEl("rect", { x: pad.left + cell.i * colW, y: pad.top + (les.indexOf(cell.le)) * rowH, width: Math.max(colW - 0.5, 0.6), height: Math.max(rowH - 0.5, 0.6), fill: color }));
  }
  les.forEach((le, idx) => {
    if (idx % Math.ceil(les.length / 6) !== 0) return;
    const t = svgEl("text", { x: pad.left - 6, y: pad.top + idx * rowH + rowH / 2 + 3, "text-anchor": "end" });
    t.textContent = byUnit(le, "s");
    svg.appendChild(t);
  });
  const { ticks, step } = timeTicks(start, end, innerW);
  for (const tk of ticks) {
    const x = pad.left + ((tk - start) / (end - start || 1)) * innerW;
    const t = svgEl("text", { x, y: height - 5, "text-anchor": "middle" });
    t.textContent = xLabelFmt(tk, step);
    svg.appendChild(t);
  }
  el.appendChild(svg);
}

export function scatter(el, points, opts = {}) {
  const width = Math.max(el.clientWidth || 640, 260);
  const height = opts.height || 170;
  const pad = { left: 44, right: 10, top: 8, bottom: 20 };
  el.innerHTML = "";
  if (!points.length) { el.appendChild(h("div", { class: "empty", style: { padding: "20px" } }, "No traces in range")); return; }
  const start = Math.min(...points.map((p) => p.start_us / 1000)), end = Math.max(...points.map((p) => p.start_us / 1000));
  const maxDur = Math.max(...points.map((p) => p.duration_ms), 10);
  const innerW = width - pad.left - pad.right, innerH = height - pad.top - pad.bottom;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, class: "chart" });
  for (const tk of niceTicks(0, maxDur, 3)) {
    const y = pad.top + innerH - (tk / maxDur) * innerH;
    svg.appendChild(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: "grid-line" }));
    const t = svgEl("text", { x: pad.left - 6, y: y + 3, "text-anchor": "end" });
    t.textContent = byUnit(tk, "ms");
    svg.appendChild(t);
  }
  for (const p of points) {
    const x = pad.left + ((p.start_us / 1000 - start) / (end - start || 1)) * innerW;
    const y = pad.top + innerH - (Math.min(p.duration_ms, maxDur) / maxDur) * innerH;
    const dot = svgEl("circle", { cx: x, cy: y, r: 3, fill: p.status === "error" ? "#ff6b6b" : "#9ae66e", opacity: 0.75 });
    dot.addEventListener("mouseenter", (ev) => {
      const tip = tooltip();
      tip.innerHTML = "";
      tip.appendChild(h("div", { class: "tt-time" }, new Date(p.start_us / 1000).toLocaleString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })));
      tip.appendChild(h("div", { class: "tt-row" }, h("span", {}, p.root), h("span", { class: "mono" }, p.duration_ms + "ms")));
      tip.appendChild(h("div", { class: "tt-row" }, h("span", {}, p.service), h("span", { class: "mono" }, p.spans + " spans")));
      tip.style.display = "block";
      tip.style.left = ev.clientX + 12 + "px";
      tip.style.top = ev.clientY - 60 + "px";
    });
    dot.addEventListener("mouseleave", hideTooltip);
    dot.addEventListener("click", () => opts.onSelect?.(p));
    dot.style.cursor = "pointer";
    svg.appendChild(dot);
  }
  el.appendChild(svg);
}

export function barList(el, rows, opts = {}) {
  const width = Math.max(el.clientWidth || 400, 200);
  const rowH = 24;
  const height = rows.length * rowH + 6;
  el.innerHTML = "";
  if (!rows.length) { el.appendChild(h("div", { class: "empty" }, "No data")); return; }
  const max = Math.max(...rows.map((r) => r.value), 1);
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height });
  const labelW = Math.min(150, width * 0.34);
  rows.forEach((r, i) => {
    const y = i * rowH + 4;
    const w = ((r.value / max) * (width - labelW - 56));
    const name = svgEl("text", { x: 0, y: y + 13, style: "font: 10.5px var(--mono); fill: var(--text-2)" });
    name.textContent = String(r.label).slice(0, 22);
    svg.appendChild(name);
    svg.appendChild(svgEl("rect", { x: labelW, y: y + 3, width: Math.max(w, 1), height: 12, rx: 3, fill: r.color || "#4b7f9e", opacity: 0.85 }));
    const val = svgEl("text", { x: labelW + w + 7, y: y + 13, style: "font: 10.5px var(--mono); fill: var(--text-3)" });
    val.textContent = opts.format ? opts.format(r.value) : r.value;
    svg.appendChild(val);
  });
  el.appendChild(svg);
}
