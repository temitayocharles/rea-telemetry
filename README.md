# TelemetryCraft

**The whole observability stack. One process. Zero dependencies.**

Grafana, Prometheus, Alertmanager, Loki, Jaeger, Sentry, PagerDuty, Statuspage, k6 and the
OpenTelemetry Collector are ten products, ten deployments and ten data models. TelemetryCraft
is one SQLite file, one Python process, and one interface that keeps the navigation you
already know — so the muscle memory transfers and the operational surface disappears.

```bash
python3 -m craft serve         # http://localhost:8787
```

No pip install, no Docker required, no external services. Python 3.10+ standard library only.
First boot generates a realistic 7-day demo environment (≈640k samples, 4k log lines, 4k spans,
an active SEV2 incident) in about four seconds, so the UI is never an empty shell.

---

## The thirteen apps

| Craft | Replaces | What actually works |
|---|---|---|
| **Overview** | Grafana home | Golden signals, service RED table, live incident banner, budgets, on-call, status |
| **Metrics** | Prometheus + Grafana Explore | A real PromQL engine: selectors, `rate`, `histogram_quantile`, aggregations, vector matching, `predict_linear`, label functions; instant + range, series table, label browser, scrape targets |
| **Dashboards** | Grafana | Folders, panels (timeseries, stat, gauge, table, bargauge, heatmap), template variables, grid layout, panel editor with live preview, JSON export |
| **Logs** | Loki + Grafana Logs | LogQL stream selectors, line filters (`\|=`, `!=`, `\|~`, `!~`), label filters, volume histogram, per-level filtering, expandable rows, live tail |
| **Traces** | Jaeger / Tempo | Trace search by service/status/operation/duration, latency scatter, service map derived from span parentage, full waterfall with span attributes, slowest-first |
| **Errors** | Sentry | Fingerprinted issue groups, stack traces with source lines, affected users, releases, hourly sparklines, resolve/ignore/assign |
| **Alerting** | Prometheus rules + Alertmanager | PromQL rules with `for` semantics (pending → firing), grouping, acknowledgement, silences with matchers, receivers, delivery log, rule editor with expression test |
| **Incidents** | PagerDuty / incident.io | Declare (SEV1–4), roles, responder roster, status transitions, timestamped timeline, blameless postmortem with action items |
| **SLOs** | Nobl9 / Sloth | SLI/total expressions, error budgets, 1h/6h/24h burn rates, projected exhaustion, breach status |
| **Synthetics** | Pingdom / Checkly + Statuspage | Multi-region HTTP checks with assertions, uptime bars, latency, live execution, and a public status page |
| **Load** | k6 / Locust | Real HTTP load against your target, bounded to 20 VUs / 60s, per-second p50/p95/p99 + error rate |
| **Collector** | OpenTelemetry Collector | Pipeline config (receivers → processors → exporters), every ingest endpoint documented with a copyable command, storage and retention controls, scrape targets |
| **Settings** | — | Organisation, receivers, retention, on-call rotations, snapshot export, demo regeneration |

Every screen is wired to the same database. Silence an alert and it shows up in the firing list;
declare an incident from an alert and it lands on the overview; resolve the incident and the
status page changes. This is not a mockup — the tests exercise the round trips.

---

## Quick start

### Run from a checkout

```bash
git clone https://github.com/temitayocharles/rea-telemetry
cd rea-telemetry
python3 -m craft serve
# open http://localhost:8787
```

The database lives at `data/telemetry.db` (git-ignored). Regenerate the demo any time:

```bash
python3 -m craft seed --force
```

### Install as a command

```bash
pip install .
telemetrycraft serve --port 8787
# or: craft serve
```

### Docker

```bash
docker compose up --build       # http://localhost:8787, data in a named volume
```

---

## Send it your own telemetry

Every ingest endpoint speaks a format agents already produce. Point what you have at it:

```bash
# Prometheus remote-write (JSON)
curl -X POST localhost:8787/api/prom/api/v1/write -H 'Content-Type: application/json' \
  -d '{"timeseries":[{"metric":{"__name__":"queue_depth","job":"mailer"},"values":[[1700000000000,"42"]]}]}'

# simple JSON
curl -X POST localhost:8787/api/metrics/write -H 'Content-Type: application/json' \
  -d '{"series":[{"metric":"queue_depth","labels":{"queue":"email"},"value":73}]}'

# Loki push
curl -X POST localhost:8787/api/loki/api/v1/push -H 'Content-Type: application/json' \
  -d '{"streams":[{"stream":{"service_name":"api"},"values":[["1700000000000000000","level=error msg=boom"]]}]}'

# OTLP (JSON encoding) — metrics, logs and traces
curl -X POST localhost:8787/api/otlp/v1/traces -H 'Content-Type: application/json' -d @trace.json

# error events (Sentry-shaped)
curl -X POST localhost:8787/api/events -H 'Content-Type: application/json' \
  -d '{"title":"TypeError","service":"web","level":"error","user_id":"u_1","stack":[{"module":"app.js","line":12}]}'
```

`/metrics` also exposes TelemetryCraft's own self-telemetry in Prometheus text format, so a real
Prometheus — or TelemetryCraft itself — can scrape the scraper.

Verified compatibility and the exact subset supported are in
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

---

## Command line

Everything the UI does is available headlessly:

```bash
craft status
craft query 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))'
craft query 'sum(rate(http_requests_total[5m])) by (service)' --range 6h
craft logs '{service="checkout-service"} |~ "(?i)timeout"' --since 2h
craft traces --service payment-service --min-duration 500
craft traces aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa          # waterfall
craft alerts --evaluate
craft alerts --ack 3 --by priya
craft incidents --declare "Checkout failing" --severity SEV2 --commander "Priya"
craft slos
craft slos --json | jq '.slos[] | {name, budget_remaining_pct}'
craft checks --run 1
craft load --target http://localhost:8080/health --vus 10 --duration 30
craft write queue_depth 91 --label queue=email
craft export --out snapshot.json
```

---

## How it works

```
                ┌──────────────────────────── one process ────────────────────────────┐
  remote-write  │                                                                     │
  OTLP / Loki   │  ingest adapters ──▶ normaliser ──▶ SQLite (WAL)                    │
  JSON / scrape │                                        │                            │
                │                             ┌──────────┴──────────┐                 │
                │                        PromQL engine       LogQL / trace search     │
                │                             │                     │                 │
                │  collector loop: alert evaluation · synthetic checks · retention    │
                │                             │                                       │
                │                      REST API (stdlib http.server)                   │
                │                             │                                       │
                │              vanilla-JS console (no build step, no framework)        │
                └─────────────────────────────────────────────────────────────────────┘
```

- **Storage.** One SQLite file in WAL mode. `samples(series_id, ts, value)` is a clustered
  `WITHOUT ROWID` table, so a range scan for one series is a B-tree seek. Logs and spans are
  plain tables with time indexes. Everything is transactional and survives restarts.
- **Query engine.** `craft/promql.py` is a lexer, Pratt-style parser and evaluator written from
  scratch (~1,100 lines). Range selectors read only the needed window; aggregations group on
  the fly; results are returned in the Prometheus HTTP API shape.
- **Demo stays live.** Once the demo is seeded, the collector extends every series forward once a
  minute (counters continue at their recent rate, gauges follow their trend) and appends fresh log
  lines and traces, so charts never flatline five minutes after boot. After a long gap the whole
  window is slid forward instead of drawing a cliff. Set `CRAFT_LIVE=0` to opt out.
- **Alerting.** The collector evaluates each rule every 15 seconds, maintains `pending`/`firing`
  state with real `for` timers, resolves series that disappear, applies silences, and records
  delivery per receiver.
- **SLOs.** Good and total expressions are sampled across the window. For windows wider than the
  rate selector, selectors are widened to the step so a 30-day SLO integrates the whole period
  instead of over-weighting the last incident.
- **UI.** Hand-written SVG charts with crosshair tooltips, resize redraws, and no dependencies.
  Hash routing, keyboard palette (<kbd>⌘K</kbd>), shared time-range state.

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Deliberately small

This is a working reimplementation, not a reskin and not a cluster. Things it does **not** try
to be:

- Not horizontally scalable. One process, one file, ~640k samples of demo data. Good to a few
  million samples and a handful of concurrent users; beyond that, use the real tools.
- Not a drop-in Prometheus replacement for federation, remote read, or recording rules.
- PromQL covers the practical language, not every corner (`@ start()/end()` are parsed and
  handled, subqueries are not).
- LogQL covers stream selection and filtering, not full metric-query composition over logs.
- OTLP is the JSON encoding; protobuf needs a decoder and is not included.
- No authentication. Run it on localhost or behind your own proxy.

The value is that you can read the whole thing in an afternoon, run it on a laptop, and still
have dashboards, alerts, traces, error tracking, and error budgets.

---

## Development

```bash
make test      # 50 tests, stdlib unittest, no pytest required
make run       # server with demo data
make seed      # regenerate demo data
make docker
```

```
craft/            engine: storage, PromQL, LogQL, alerting, incidents, SLOs, synthetics, API
craft/server.py   HTTP API + collector loop
craft/seed.py     7-day demo environment generator
craft/web/        the console (index.html, css/app.css, js/apps/*)
tests/            unit + end-to-end API tests
docs/             architecture, compatibility, API reference
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — storage layout, query engine, alerting, SLO maths
- [Compatibility](docs/COMPATIBILITY.md) — the exact PromQL, LogQL, OTLP and API subset supported
- [HTTP API](docs/API.md) — every endpoint with request and response shapes
- [CLI reference](docs/CLI.md) — every command, plus scripting recipes

MIT licensed. See [LICENSE](LICENSE).
