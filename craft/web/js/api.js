// Thin API client. Same-origin, no auth (local-first), JSON everywhere.
import { toast } from "./ui.js";

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const message = (data && data.error) || `${res.status} ${res.statusText}`;
    if (!options.silent) toast(message, "err");
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

const qs = (params = {}) => {
  const clean = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return clean.length ? "?" + new URLSearchParams(clean).toString() : "";
};

export const api = {
  meta: () => request("/api/meta"),
  overview: () => request("/api/overview"),
  stats: () => request("/api/stats"),

  // Prometheus
  query: (query, time) => request(`/api/prom/api/v1/query${qs({ query, time })}`),
  queryRange: (query, start, end, step) => request(`/api/prom/api/v1/query_range${qs({ query, start, end, step })}`),
  labels: () => request("/api/prom/api/v1/labels"),
  labelValues: (name) => request(`/api/prom/api/v1/label/${encodeURIComponent(name)}/values`),
  metrics: () => request("/api/explorer/metrics"),
  metricSeries: (metric) => request(`/api/explorer/series${qs({ metric })}`),
  targets: () => request("/api/explorer/targets"),
  saveTargets: (targets) => request("/api/explorer/targets", { method: "POST", body: { targets } }),
  scrape: (target) => request("/api/explorer/scrape", { method: "POST", body: target }),

  // Logs
  logs: (query, start, end, limit = 200, direction = "backward") => request(`/api/logs${qs({ query, start, end, limit, direction })}`),
  logVolume: (query, start, end, step) => request(`/api/logs/volume${qs({ query, start, end, step })}`),
  logStats: (query, start, end) => request(`/api/logs/stats${qs({ query, start, end })}`),
  logFields: () => request("/api/logs/fields"),

  // Traces
  traces: (params) => request(`/api/traces${qs(params)}`),
  trace: (id) => request(`/api/traces/${id}`),
  traceAggregates: (start, end, groupBy) => request(`/api/traces/aggregates${qs({ start, end, group_by: groupBy })}`),
  traceVolume: (start, end, step) => request(`/api/traces/volume${qs({ start, end, step })}`),
  serviceGraph: (start, end) => request(`/api/traces/graph${qs({ start, end })}`),

  // Errors
  issues: (params) => request(`/api/issues${qs(params)}`),
  issue: (id) => request(`/api/issues/${id}`),
  issueStatus: (id, status) => request(`/api/issues/${id}/status`, { method: "POST", body: { status } }),
  issueAssign: (id, assigned) => request(`/api/issues/${id}/assign`, { method: "POST", body: { assigned } }),

  // Alerts
  alerts: (state) => request(`/api/alerts${qs({ state })}`),
  evaluateAlerts: () => request("/api/alerts/evaluate", { method: "POST" }),
  ackAlert: (id, by) => request(`/api/alerts/${id}/ack`, { method: "POST", body: { by } }),
  alertToIncident: (id) => request(`/api/alerts/${id}/incident`, { method: "POST", body: {} }),
  rules: () => request("/api/rules"),
  saveRule: (rule) => request("/api/rules", { method: "POST", body: rule }),
  deleteRule: (id) => request(`/api/rules/${id}`, { method: "DELETE" }),
  previewRule: (expr) => request("/api/rules/preview", { method: "POST", body: { expr } }),
  silences: () => request("/api/silences"),
  createSilence: (body) => request("/api/silences", { method: "POST", body }),
  deleteSilence: (id) => request(`/api/silences/${id}`, { method: "DELETE" }),
  notifications: () => request("/api/notifications"),
  receivers: () => request("/api/receivers"),
  saveReceivers: (receivers) => request("/api/receivers", { method: "POST", body: { receivers } }),
  testReceiver: (receiver) => request("/api/receivers/test", { method: "POST", body: { receiver } }),
  onCall: () => request("/api/oncall"),
  schedules: () => request("/api/schedules"),
  saveSchedule: (s) => request("/api/schedules", { method: "POST", body: s }),

  // SLOs
  slos: () => request("/api/slos"),
  saveSlo: (slo) => request("/api/slos", { method: "POST", body: slo }),
  deleteSlo: (id) => request(`/api/slos/${id}`, { method: "DELETE" }),

  // Synthetics
  checks: () => request("/api/checks"),
  saveCheck: (c) => request("/api/checks", { method: "POST", body: c }),
  deleteCheck: (id) => request(`/api/checks/${id}`, { method: "DELETE" }),
  runCheck: (id) => request(`/api/checks/${id}/run`, { method: "POST", body: {} }),
  statusPage: () => request("/api/status-page"),

  // Incidents
  incidents: (status) => request(`/api/incidents${qs({ status })}`),
  incident: (id) => request(`/api/incidents/${id}`),
  declareIncident: (body) => request("/api/incidents", { method: "POST", body }),
  incidentAction: (id, action, body) => request(`/api/incidents/${id}/${action}`, { method: "POST", body }),

  // Dashboards
  dashboards: () => request("/api/dashboards"),
  dashboard: (uid) => request(`/api/dashboards/${uid}`),
  saveDashboard: (d) => request("/api/dashboards", { method: "POST", body: d }),
  deleteDashboard: (uid) => request(`/api/dashboards/${uid}`, { method: "DELETE" }),
  starDashboard: (uid) => request(`/api/dashboards/${uid}/star`, { method: "POST", body: {} }),

  // Load
  loadtests: () => request("/api/loadtests"),
  startLoadtest: (body) => request("/api/loadtests", { method: "POST", body }),
  cancelLoadtest: (id) => request(`/api/loadtests/${id}/cancel`, { method: "POST", body: {} }),

  // Collector / settings
  pipelines: () => request("/api/pipelines"),
  savePipeline: (p) => request("/api/pipelines/save", { method: "POST", body: p }),
  settings: () => request("/api/settings"),
  saveSettings: (s) => request("/api/settings", { method: "POST", body: s }),
  prune: () => request("/api/collector/prune", { method: "POST", body: {} }),

  // Misc
  search: (q) => request(`/api/search${qs({ q })}`),
  seedDemo: (force = false) => request("/api/demo/seed", { method: "POST", body: { force } }),
  exportAll: () => request("/api/export"),
};
