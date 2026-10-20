# Architecture

TelemetryCraft is a single Python process with one SQLite file behind it. This document explains
how data gets in, how it is stored, how queries and alerting work, and where the boundaries are.

```
                 ┌─────────────────────── one process ─────────────────────────┐
 remote-write    │                                                              │
 OTLP / Loki     │  ingest adapters ──▶ normaliser ──▶ SQLite (WAL)             │
 JSON / scrape   │                                        │                     │
                 │                             ┌──────────┴──────────┐          │
                 │                        PromQL engine       LogQL / traces    │
                 │                             │                     │          │
                 │  collector loop: alert eval · synthetic checks · retention   │
                 │                             │                                │
                 │                     REST API (http.server)                   │
                 │                             │                                │
                 │              vanilla-JS console (no build step)              │
                 └──────────────────────────────────────────────────────────────┘
```

## Layers and dependency direction

```
__main__ / cli  ──▶  server  ──▶  { query, alerting, slo, synthetics, incidents,
                                     dashboards, loadtest, store }
                                     │
                                     ▼
                                   promql
                                     │
                                     ▼
                                    db
```

Dependencies only ever point downward. `db.py` knows nothing about metrics; `promql.py` knows
nothing about HTTP; `server.py` contains no query logic. This is what makes the CLI, the test
suite and the UI all able to talk to the same engine without duplication.

| Module | Responsibility |
|---|---|
| `db.py` | Schema, connection (WAL, shared connection, `REGEXP` UDF), `reset()`, `now()`, meta key/value, JSON helpers |
| `promql.py` | Lexer, parser, evaluator for PromQL; instant and range queries; label discovery |
| `store.py` | All ingestion paths, `MetricWriter`, scrape targets, retention `prune()`, `stats()` |
| `query.py` | LogQL subset, log search, trace search/aggregates/service graph, global search |
| `alerting.py` | Rules, silences, evaluation state machine, acknowledgements, receivers, on-call |
| `slo.py` | SLI evaluation, error budgets, multi-window burn rates |
| `synthetics.py` | HTTP checks with assertions, result history, status page |
| `incidents.py` | Incident lifecycle, timeline events, postmortem document |
| `dashboards.py` | Dashboard CRUD, default dashboards, panel helpers |
| `loadtest.py` | Bounded concurrent HTTP load generation, percentile aggregation |
| `server.py` | Route table, static file serving, collector thread, snapshot export |
| `cli.py` | Argparse front-end over the same engine |

## Storage

One file, SQLite, WAL mode, `synchronous=NORMAL`. A single shared connection is used by the
server and the collector thread; the GIL plus SQLite's own locking make this safe for the
concurrency levels this tool targets.

```
series (id, metric, labels_json, fingerprint UNIQUE, last_seen)
samples (series_id, ts, value)  WITHOUT ROWID, PK (series_id, ts)
logs    (id, ts, service, level, body, attrs_json, trace_id)
spans   (trace_id, span_id, parent_id, name, service, ...)  WITHOUT ROWID, PK (trace_id, span_id)
issues, events              error grouping and per-event payloads
rules, alert_instances, silences, notifications
schedules, oncall           on-call rotations
services, slos              SLIs and error budgets
checks, check_results       synthetic monitoring
incidents, incident_events  incident lifecycle
dashboards, saved_searches, loadtests, loadtest_points, pipelines, settings
```

Design notes:

- **Samples are clustered by series**, so `range(series_id, ts?)` is a B-tree seek followed by a
  sequential scan — no bitmap intersections, no per-sample row overhead beyond the tuple.
  A secondary index on `ts` serves retention deletes and cross-series time filters.
- **Everything is stored in seconds** (`REAL`), matching Prometheus. Ingestion adapters
  normalise whatever they receive: OTLP nanoseconds ÷ 1e9, Loki nanoseconds ÷ 1e9, remote-write
  and `query_range` milliseconds ÷ 1e3. `MetricWriter.add()` guards against callers passing
  milliseconds by dividing anything above 1e11.
- **Series identity is the metric name plus a canonical, sorted label JSON**, hashed to a
  fingerprint. Labels live once per series, not once per sample.
- **Timestamps come from `db.now()`**, a single epoch-seconds helper, so seeding, injection and
  queries agree on the clock.
- Growth: about 40 bytes per sample on disk. The demo environment (≈640k samples, 4.2k logs,
  4k spans) is a 40 MB file.

## Ingestion

Three shapes, all normalised into the same tables:

1. **Prometheus** — remote-write JSON (`{"timeseries": [...]}`), the Prometheus text exposition
   format (via `/metrics` scrape or `POST` body), and the simple `{"series": [...]}` JSON form.
2. **Logs** — Loki push (`{"streams": [...]}`), the simple `{"logs": [...]}` JSON form, and
   OTLP logs. Log bodies are parsed for `level=`/`severity` to populate the level column.
3. **Traces** — OTLP traces (JSON encoding), including events and span attributes. Parentage is
   preserved so the service graph can be derived later.

Errors arrive as Sentry-shaped events; `ingest_event()` fingerprints the stack and either
attaches to an existing issue or opens a new one.

Ingestion is idempotent-ish in the sense that duplicate samples at the same timestamp replace
each other (`INSERT OR REPLACE` on the composite key), so retries are safe.

## Query engine

`promql.py` is a hand-written lexer → recursive-descent parser → evaluator.

- **Vectors.** An instant vector is a list of `Sample(metric: dict, value: float)`. A range
  vector is `[(metric, [(ts, v), ...])]`. Matrices are `[(metric, values)]` for the HTTP API.
- **Evaluation.**
  - A selector with a range (`foo[5m]`) materialises the series window into SQL.
  - Functions compute over the window: `rate`/`increase` divide by the actual span between the
    first and last sample (not the nominal range), counter resets are counted and compensated,
    `irate` uses the last two points, `predict_linear` does least-squares over the window.
  - Aggregations group by `by(...)`/`without(...)` labels or the full set when ungrouped.
  - Binary operators implement scalar/vector/vector arithmetic with `on`/`ignoring` matching,
    `group_left`/`group_right` many-to-one, and comparison operators (filtering by default,
    `bool` when asked).
  - `offset` shifts the evaluation instant; `@` pins it.
- **Range queries** evaluate the parsed AST once per step, sharing the parsed tree but not the
  data, so a 7-day query at 60 s costs 10,080 evaluations — acceptable in SQLite because each
  evaluation touches only its window.
- **Results** are shaped exactly like the Prometheus HTTP API (`resultType`, `result`,
  `values` as `[ts, "string"]`) so Grafana-style clients and the UI helpers work unchanged.

## Alerting

The collector thread wakes every 15 seconds and:

1. Resolves each enabled rule's expression at the current instant.
2. Fingerprints `blake2b(rule_name + sorted labels)` to find or create an alert instance.
3. Extends the seeded demo environment forward (once a minute) so the console never flatlines:
   counters continue at their recent rate, gauges follow their trend, and fresh logs and traces are
   appended. If the data has fallen more than three minutes behind (laptop asleep, container
   paused), the whole window is rebased instead of drawing a gap. `CRAFT_LIVE=0` disables this.
4. Applies the state machine: `inactive → pending → firing` with a wall-clock `for` timer,
   `firing → resolved` when the series disappears or the expression goes false.
5. Applies silences (matchers on rule name and labels, with start/end times) — silenced alerts
   are recorded but not delivered.
6. Records one `notifications` row per receiver with the payload, so delivery is auditable even
   though no external paging service is contacted.

Acknowledgement is per instance; an unacknowledged firing alert rolls up to the overview banner.
Any firing alert can be promoted to an incident in one request.

## SLOs

An SLO has a good-events expression, a total-events expression, a target ratio and a window in
days. Evaluation samples the window and computes:

- `availability = good / total`
- `budget_remaining = 1 - (1 - availability) / (1 - target)`
- burn rates over 1 h, 6 h and 24 h relative to the budget consumption rate implied by the target.

Plotting/evaluation widens each `[...]` selector to the evaluation step when the step is longer
than the selector, so a 30-day window integrates the whole period rather than over-weighting
the most recent minutes (a bug that silently inflates SLIs in naive implementations).

## Synthetics and status

Checks are HTTP requests with method, headers, body, expected status and assertion expressions.
Each run records status, latency, and the response code. Uptime bars and p95 latency come from
those rows. The status page aggregates the last 24 hours per check into operational /
degraded / outage.

## Collector and load generation

`loadtest.py` runs a bounded number of worker threads (hard-capped at 20 VUs, 60 seconds) against
a target URL, writing one aggregated point per second (p50/p95/p99, error rate, RPS). It exits
deterministically and can be cancelled. It is a smoke/load tool, not a distributed load
generator.

## HTTP layer

`http.server.ThreadingHTTPServer` with a small router: exact paths for fixed endpoints, a handful
of regexes for `/<resource>/<id>` forms. Query strings are parsed into `{key: [values]}` lists.
JSON in, JSON out; errors are `{"error": "..."}` with a non-200 status. Static files are served
from `craft/web/` with a `text/html` SPA fallback for unknown paths. There is no authentication and no
CORS restriction — bind to localhost or put it behind your own proxy.

## Testing

`tests/` uses only `unittest`:

- `test_promql.py` builds a deterministic fixture (counters incrementing by exactly 1 per 15 s so
  `rate` is analytically known) and asserts function, aggregation and label behaviour.
- `test_ingest_alerting.py` exercises every ingest path, then the alert state machine, silences,
  incident promotion, retention, synthetic checks and the status page.
- `test_live.py` covers the demo continuation: appending at the recent rate, clamping gauges to
  sane ranges, rebasing after a multi-hour gap, and the disabled/no-data no-ops.
- `test_api.py` boots a real `ThreadingHTTPServer` on an ephemeral port against a seeded temporary
  database and walks ~20 endpoints, so routing, JSON shapes and HTTP status codes are covered
  end to end.

Each class resets the database in `setUpClass` and isolates by label — no cross-test ordering
dependencies.
