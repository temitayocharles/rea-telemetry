# Compatibility

What TelemetryCraft accepts and what it answers, signal by signal — and, just as importantly,
what it does not. Everything below is exercised by the test suite.

---

## Prometheus

### HTTP API

| Endpoint | Params | Notes |
|---|---|---|
| `GET /api/prom/api/v1/query` | `query`, `time` (ms) | Instant query. Returns `{"status":"success","data":{"resultType":"vector","result":[…]}}` |
| `POST /api/prom/api/v1/query` | form or JSON body | Same, for clients that POST |
| `GET /api/prom/api/v1/query_range` | `query`, `start`, `end`, `step` | `step` accepts `60`, `60s`, `1m` |
| `GET /api/prom/api/v1/series` | `match[]` | Selector only |
| `GET /api/prom/api/v1/labels` | — | Sorted label names |
| `GET /api/prom/api/v1/label/<name>/values` | — | Sorted values |
| `GET /api/prom/api/v1/metadata` | — | Metric names, types, help |
| `POST /api/prom/api/v1/write` | JSON body | Remote-write shaped (see below) |

Series/vector elements use the canonical shape `{"metric": {...}, "value": [ts, "1.5"]}` and
`{"metric": {...}, "values": [[ts, "1.5"], …]}` — values are **strings**, as Prometheus returns
them, and timestamps are seconds. The UI helpers accept both the Prometheus nesting
(`res.data.result`) and a flat shape, so the same code works against a real Prometheus.

```bash
curl -s 'localhost:8787/api/prom/api/v1/query' \
  --data-urlencode 'query=sum(rate(http_requests_total[5m])) by (service)'
```

### Remote write

JSON encoding of the remote-write message — `{"timeseries":[{"metric":{"__name__":…,"job":…},
"values":[[epoch_ms,"value"],…]}]}`. Timestamps are milliseconds here (as in the real protocol)
and are converted on ingest. Protobuf/snappy encoding is **not** supported.

```bash
curl -sX POST localhost:8787/api/prom/api/v1/write -H 'Content-Type: application/json' \
  -d '{"timeseries":[{"metric":{"__name__":"queue_depth","queue":"email"},
       "values":[[1700000000000,"42"]]}]}'
```

Prometheus text exposition (`# TYPE`/`# HELP`/`metric{labels} value ts`) is accepted as a body on
`/api/prom/api/v1/write` and by the scrape loop. `/metrics` serves TelemetryCraft's own metrics in
that format, so it can scrape itself.

### PromQL

**Selectors and matching** — `metric`, `{__name__="m"}`, all four label operators (`=`, `!=`,
`=~`, `!~`, fully anchored regexes), multiple matchers, `offset <duration>`, `@ <timestamp>`,
range selectors `[5m]`, `[1h30m]`, `[7d]`, duration arithmetic with `ms s m h d w y`.

**Aggregators** — `sum avg min max count stddev stdvar quantile group topk bottomk
count_values limitk limit_ratio`, with `by (…)` and `without (…)`. Ungrouped aggregation keeps the
full label set, matching Prometheus.

**Binary operators** — `+ - * / % ^`, comparisons `== != > < >= <=` (filtering, plus `bool`),
set operators `and or unless`, vector matching with `on(…)`/`ignoring(…)` and
`group_left`/`group_right`.

**Range-vector functions** — `rate irate increase delta idelta deriv resets changes
avg_over_time min_over_time max_over_time sum_over_time count_over_time last_over_time
quantile_over_time stddev_over_time stdvar_over_time absent_over_time present_over_time
predict_linear`.

**Instant-vector functions** — `abs ceil floor round sqrt exp ln log2 log10 sgn clamp clamp_min
clamp_max sort sort_desc sort_by_label timestamp absent histogram_quantile label_replace label_join
vector scalar time pi day_of_week hour minute month year days_in_month`.

**Not implemented:** subqueries (`expr[5m:1m]`), `holt_winters`/`double_exponential_smoothing`,
native histograms, `@` inside range selectors combined with subqueries, recording rules, remote
read, and federation. `histogram_quantile` expects classic `_bucket` series with an `le` label.

Semantics worth knowing:

- `rate`/`increase` divide by the *actual* interval between the first and last sample in the
  window, and compensate for counter resets — same as Prometheus.
- `predict_linear(v[1h], 3600 * 4)` is least-squares over the window; a series already past its
  threshold yields a large negative number, which is a valid (if alarming) extrapolation rather
  than a bug.
- A range query evaluates once per step; `[5m]` windows are read from storage per step.

---

## Loki

`POST /api/loki/api/v1/push` accepts the Loki push JSON:

```json
{"streams":[{"stream":{"service_name":"checkout","level":"error"},
             "values":[["1700000000000000000","level=error msg=timeout"]]}]}
```

Timestamps are nanoseconds and are converted on ingest. The UI and `craft logs` speak LogQL:

| Syntax | Supported |
|---|---|
| `{service="api"}` | ✅ equality and all three regex/negation matchers |
| `{service=~"check.*"} \|= "timeout"` | ✅ line contains |
| `\|=`, `!=`, `\|~`, `!~` | ✅ regex where applicable, case-sensitive unless `(?i)` |
| `{…} \| json` / `\| logfmt` | ✅ parsed into fields available to label filters |
| `{…} \| level="error"` | ✅ label filter after parser |
| `sum(count_over_time({…} [5m]))` | ✅ for the volume histogram |
| `\| line_format`, `\| label_format` | parsed and ignored (display is unchanged) |
| Metric queries like `rate({…}[5m])` | ❌ not over logs |

Responses use the Loki shape: `{"streams":[{"stream":{…},"values":[[ns, line], …]}]}`, plus
`/api/logs/volume`, `/api/logs/stats` and `/api/logs/fields` for the console.

---

## OpenTelemetry

OTLP over **HTTP with JSON encoding** on both `/api/otlp/v1/{signal}` and the standard root paths
`/v1/{signal}`:

| Signal | Path |
|---|---|
| Metrics | `POST /api/otlp/v1/metrics`, `POST /v1/metrics` |
| Logs | `POST /api/otlp/v1/logs`, `POST /v1/logs` |
| Traces | `POST /api/otlp/v1/traces`, `POST /v1/traces` |

Gauge, sum and histogram data points are normalised into the metric model (histograms stored as
`_bucket`/`_count`/`_sum` series so `histogram_quantile` works). Traces keep `trace_id`,
`span_id`, `parent_span_id`, service, operation, duration in microseconds, status and attributes,
so the service graph and waterfall are derived rather than stored.

Protobuf encoding is **not** supported — configure exporters with `protocol: http/json`, e.g.:

```yaml
exporters:
  otlphttp:
    endpoint: http://localhost:8787
    encoding: json
```

The Collector app documents this with copyable commands.

---

## Error events (Sentry-shaped)

`POST /api/events` accepts:

```json
{"title":"TypeError: cannot read properties of undefined",
 "service":"web-frontend", "level":"error", "environment":"production",
 "release":"v2.14.0", "user_id":"u_4821",
 "message":"...", "stack":[{"module":"app.js","line":12,"function":"render"}]}
```

Events are fingerprinted (title + service + top stack frames) and grouped into issues with
`count`, `users`, `first_seen`, `last_seen`, status (`unresolved`/`resolved`/`ignored`) and
assignment. `/api/issues`, `/api/issues/<id>`, `/api/issues/<id>/status`, `/api/issues/<id>/assign`.

---

## Everything else

| Shape | Endpoint |
|---|---|
| Simple metric JSON | `POST /api/metrics/write` `{"series":[{"metric":"m","labels":{…},"value":1,"ts":…}]}` |
| Simple log JSON | `POST /api/logs` `{"logs":[{"service":"api","level":"info","body":"…"}]}` |
| Simple trace JSON | `POST /api/traces` `{"spans":[…]}` |
| Scrape Prometheus targets | `POST /api/explorer/scrape` (target URL or a configured target id) |

Outbound integrations are deliberately inert: receivers (webhook, Slack, PagerDuty, email) record
a delivery row in the notification log instead of contacting a third party, so nothing leaves the
machine unless you point a webhook at something you control. Load tests make real HTTP requests to
the target you name — that is the one outbound path by design.
