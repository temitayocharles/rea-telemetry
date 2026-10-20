# HTTP API

Everything the console does, the API does. Responses are JSON; errors are
`{"error": "message"}` with status `400` (bad input), `404` (unknown route/resource) or `500`.
Timestamps are epoch **seconds** unless a parameter name says `_ms` or `start`/`end` in a
Prometheus route (milliseconds, per the Prometheus spec).

Base URL: `http://localhost:8787`.

---

## Authentication

Off by default. A server started with `--auth-token` or `--auth-user/--auth-password` (or the
`CRAFT_AUTH_TOKEN` / `CRAFT_AUTH_USER` / `CRAFT_AUTH_PASSWORD` variables) requires a credential on
every `/api/*` and `/metrics` request. The static SPA stays readable so the browser can render the
sign-in form.

```bash
curl -H "Authorization: Bearer $TOKEN"  http://localhost:8787/api/stats
curl -u ops:secret                     http://localhost:8787/api/stats
curl "$API/api/stats?token=$TOKEN"     # convenience for scrape configs and scripts
```

Failures return `401` with a `WWW-Authenticate` challenge; after 10 failures inside a minute from
the same client the answer is `429` until a request succeeds. Comparison is constant-time.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/auth/status` | `{enabled, method, ok}`; `method` is `token`, `basic`, `token+basic` or `none`. `ok` reflects the credential you sent. |
| POST | `/api/auth/login` | Body `{token}` or `{user, password}` → `{ok, method, credential}`. The credential is the exact header value to replay; it is not a session. |

`/api/health`, `/api/status/health`, `/api/auth/status` and `/api/auth/login` are always public, so
health checks keep working behind the lock. TLS is terminated by the server itself — see
[`CLI.md`](CLI.md#serve).

## Health and meta

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","service":"telemetrycraft","version":…,"uptime_s":…}` |
| GET | `/api/status/health` | alias of `/api/health` |
| GET | `/api/meta` | version, org, app catalogue, seed version |
| GET | `/api/stats` | `{series, samples, samples_1h, logs, spans, span_errors, issues, alerts_firing, alerts_pending, db_bytes, retention_days, engine}` |
| GET | `/metrics` | Self-telemetry in Prometheus text format |
| GET | `/api/overview` | Home-page payload: headline RED metrics, 6 h series, service table, alerts, SLOs, incidents, status |

---

## Prometheus-compatible

See [COMPATIBILITY.md](COMPATIBILITY.md#prometheus) for the supported PromQL subset.

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/prom/api/v1/query` | `query`, optional `time` (ms) |
| GET/POST | `/api/prom/api/v1/query_range` | `query`, `start`, `end`, `step` |
| GET | `/api/prom/api/v1/series` | `match[]` selector |
| GET | `/api/prom/api/v1/labels` | label names |
| GET | `/api/prom/api/v1/label/<name>/values` | label values |
| GET | `/api/prom/api/v1/metadata` | metric metadata |
| POST | `/api/prom/api/v1/write` | remote-write JSON or Prometheus text |

```bash
curl -s 'localhost:8787/api/prom/api/v1/query' \
  --data-urlencode 'query=topk(3, sum(rate(http_requests_total[5m])) by (service))'
```

---

## Ingestion

| Method | Path | Body |
|---|---|---|
| POST | `/api/metrics/write` | `{"series":[{"metric":"name","labels":{…},"value":1.5,"ts":1700000000}]}` |
| POST | `/api/logs` | `{"logs":[{"service":"api","level":"error","body":"text","ts":…}]}` |
| POST | `/api/traces` | `{"spans":[{…}]}` |
| POST | `/api/loki/api/v1/push` | Loki push JSON |
| POST | `/api/otlp/v1/{metrics,logs,traces}`, `/v1/{…}` | OTLP-JSON |
| POST | `/api/events` | Sentry-shaped error event |

Ingest responses are counts: `{"ingested": 12}`.

---

## Metrics explorer

| Method | Path | Notes |
|---|---|---|
| GET | `/api/explorer/metrics` | Metric names with series counts |
| GET | `/api/explorer/series` | `metric=…` → matching series and label sets |
| GET | `/api/explorer/targets` | Configured scrape targets |
| POST | `/api/explorer/scrape` | `{"target": "http://host:9100/metrics"}` — scrape once now |

---

## Logs

| Method | Path | Params |
|---|---|---|
| GET | `/api/logs` | `query` (LogQL subset), `start`, `end`, `limit`, `direction` |
| GET | `/api/logs/volume` | `query`, `start`, `end`, `step` → histogram buckets |
| GET | `/api/logs/stats` | counts by level and service for the window |
| GET | `/api/logs/fields` | known services and levels |

```bash
curl -s 'localhost:8787/api/logs' \
  --data-urlencode 'query={service="checkout-service"} |= "timeout"' \
  --data-urlencode 'start=1700000000' --data-urlencode 'end=1700003600'
```

Response: `{"streams":[{"stream":{"service":…},"values":[[ts,"line"],…]}]}`.

---

## Traces

| Method | Path | Params |
|---|---|---|
| GET | `/api/traces` | `service`, `operation`, `status`, `min_duration_ms`, `max_duration_ms`, `tag`, `start`, `end`, `limit`, `sort` |
| GET | `/api/traces/<trace_id>` | Full trace: spans, attributes, events, duration |
| GET | `/api/traces/aggregates` | `group_by=service\|operation\|status`, `start`, `end` |
| GET | `/api/traces/volume` | throughput/latency histogram over time |
| GET | `/api/traces/graph` | Service graph derived from span parentage: nodes and edges with call counts and error rates |

---

## Errors

| Method | Path | Notes |
|---|---|---|
| GET | `/api/issues` | `status`, `service`, `query`, `sort` |
| GET | `/api/issues/<id>` | Issue plus sampled events and stack traces |
| POST | `/api/issues/<id>/status` | `{"status":"resolved"\|"unresolved"\|"ignored"}` |
| POST | `/api/issues/<id>/assign` | `{"assigned":"…"}` |

---

## Alerting

| Method | Path | Notes |
|---|---|---|
| GET | `/api/alerts` | `state=firing\|pending\|resolved`; includes `last_eval`. Each row carries `value` (raw float), `value_display` (rendered in the rule's unit), `value_note` (why a projection is negative) and `format` |
| POST | `/api/alerts/evaluate` | Evaluate all rules now, returns transitions |
| POST | `/api/alerts/<id>/ack` | `{"by":"name"}` |
| POST | `/api/alerts/<id>/incident` | Promote to an incident |
| GET/POST | `/api/rules` | List / create or update a rule |
| DELETE | `/api/rules/<id>` | Remove |
| POST | `/api/rules/preview` | `{"expr":"…"}` → current result without saving |
| GET/POST | `/api/silences` | List / create `{"matchers":[…],"duration_s":7200,"comment":"…"}` |
| DELETE | `/api/silences/<id>` | Expire now |
| GET | `/api/notifications` | Delivery log, newest first |
| GET/POST | `/api/receivers` | Routing configuration |
| POST | `/api/receivers/test` | `{"receiver":"slack"}` → dry-run delivery |
| GET | `/api/oncall` | Current on-call per schedule |
| GET/POST | `/api/schedules` | Rotations and members |

`format` is one of `""` (raw number), `bytes`, `percent`, `ratio`, `seconds`, `number`. It only
changes how the value is rendered — the stored value is always the raw result of the expression.

Rule body:

```json
{"name":"HighErrorRate","expr":"sum(rate(http_requests_total{status=~\"5..\"}[5m])) by (service) / sum(rate(http_requests_total[5m])) by (service) > 0.05",
 "for":120,"severity":"critical","summary":"5xx rate above 5%","format":"ratio","enabled":true}
```

---

## Incidents

| Method | Path | Notes |
|---|---|---|
| GET | `/api/incidents` | `status=open\|resolved\|all` |
| POST | `/api/incidents` | `{"title","severity","impact","commander","services","summary"}` |
| GET | `/api/incidents/<id>` | Incident, timeline and postmortem |
| POST | `/api/incidents/<id>/status` | `{"status":"investigating"\|"identified"\|"monitoring"\|"resolved"}` |
| POST | `/api/incidents/<id>/events` | Timeline comment |
| POST | `/api/incidents/<id>/commander` | Reassign commander |
| POST | `/api/incidents/<id>/responders` | Add responder |
| POST | `/api/incidents/<id>/postmortem` | Save blameless postmortem and action items |
| POST | `/api/incidents/from-alert` | `{"alert_id": 12}` |

Severities are `SEV1`–`SEV4`.

---

## SLOs, synthetics, status page

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/slos` | List / create. Row: `{name, service, objective, window_days, availability, budget_used_pct, budget_remaining_pct, burn_rate_1h/6h/24h, status}` |
| DELETE | `/api/slos/<id>` | Remove |
| GET/POST | `/api/checks` | Synthetic checks (URL, method, interval, assertions, regions) |
| DELETE | `/api/checks/<id>` | Remove |
| POST | `/api/checks/<id>/run` | Execute immediately and record the result |
| GET | `/api/status-page` | Public status: components, 24 h uptime, incidents |

---

## Saved searches and on-call

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/searches` | List / save `{"app":"search","name":"…","query":"…"}` |
| DELETE | `/api/searches/<id>` | Remove a saved search |
| POST | `/api/oncall/page` | `{"person","title","message"}` — records a manual page in the delivery log |

## Dashboards, search, load, collector, settings

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/dashboards` | List / save (uid upsert) |
| GET/DELETE | `/api/dashboards/<uid>` | Read / delete |
| POST | `/api/dashboards/<uid>/star` | Toggle star |
| GET | `/api/search?q=` | Cross-signal search: logs, traces, issues, alerts, dashboards |
| GET/POST | `/api/loadtests` | List / start `{"target","vus","duration_s","ramp","method"}` |
| POST | `/api/loadtests/<id>/cancel` | Stop a running test |
| GET | `/api/pipelines` | Collector pipeline config |
| POST | `/api/pipelines/save` | Persist pipeline config |
| GET/POST | `/api/settings` | Org, collector, receivers, retention, storage, endpoint list |
| GET | `/api/collector/stats` | Storage/ingest statistics |
| POST | `/api/collector/prune` | Apply retention now |
| POST | `/api/demo/seed` | Regenerate demo data (`{"force":true}`) |
| POST | `/api/demo/reset` | Clear generated state |
| GET | `/api/export` | Full JSON snapshot — dashboards, rules, SLOs, checks, incidents |

```bash
# start a bounded load test
curl -sX POST localhost:8787/api/loadtests -H 'Content-Type: application/json' \
  -d '{"name":"checkout spike","target":"http://localhost:8787/api/health","vus":10,"duration_s":20}'
```

---

## Static and SPA

`GET /`, `GET /index.html`, and any non-`/api/` path serve the console from `craft/web/`. Unknown paths
fall back to `index.html` so hash routes and deep links work. Static files are always public —
that is what makes the in-page sign-in form possible when auth is enabled — and TLS, when
configured, is terminated by the server itself.
