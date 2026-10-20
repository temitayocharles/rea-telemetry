# Handoff brief

This file is a task brief for a second agent (or a second human) working on
TelemetryCraft. It assumes you have the repository checked out and can run
Python 3.10+ and Node 18+. Read [`README.md`](README.md) first, then
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and
[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) — the last one is the honest
list of what is and is not implemented, and it is the document you will be
updating.

Two workstreams, independent of each other:

- **A. Real-browser verification.** The console has only ever been verified
  structurally (Python HTTP tests + a jsdom smoke test). Nobody has looked at
  it in a real engine.
- **B. Protocol gaps.** OTLP protobuf, Prometheus recording rules, and (as a
  stretch) LogQL metric queries.

Do them in that order if you are one agent: A is cheap, and anything A finds
changes what B has to test against.

---

## Ground rules

These are not preferences, they are the shape of the project. Breaking them
means the change cannot be merged.

1. **`craft/` is Python 3.10+ standard library only.** No `pip install`
   anything, no vendored wheels. If a feature seems to need a dependency, the
   answer is to write the 150 lines yourself or to declare the feature out of
   scope.
2. **The browser app has zero external assets.** No CDNs, no fonts, no
   frameworks, no `<script src="https://…">`. Vanilla JS modules under
   `craft/web/js/`, SVG charts drawn by hand.
3. **One SQLite file, one process.** Schema changes go in `craft/db.py` as a
   `MIGRATIONS` entry; `ALTER TABLE` anywhere else will not run on an existing
   database.
4. **Dependency direction is inward.** `store` → `db`; `promql`/`query`/`slo`
   → `store`; `server`/`cli` → everything. Nothing in the engine imports the
   server. Keep `python3 -c "import craft.server"` working at every commit.
5. **Every behaviour gets a test** in `tests/`, `unittest` only, no pytest, no
   network. The suite must stay green: `python3 -m unittest discover -s tests`
   (91 tests at the time of writing).
6. **Honesty in docs.** If you do not implement something, say so in
   `docs/COMPATIBILITY.md`. The value of that file is that it can be trusted.
7. **No data in git.** `data/*.db` is generated; the demo is recreated with
   `python3 -m craft seed --force`.
8. **Commit in coherent units** with a message that explains *why*. Do not
   reformat files you are not changing.

Quick sanity loop before every commit:

```bash
python3 -m unittest discover -s tests      # must be OK
node tests/grid_check.mjs                  # must exit 0
python3 -c "import craft.server, craft.cli, craft.auth"
python3 -m craft serve --port 8787 &       # then click around http://localhost:8787
```

---

## Workstream A — real-browser verification

### Why this exists

In the sandbox this repository was built in, no headless browser could be
installed: no system Chromium, no `libnspr4`, and the Playwright CDN is not
reachable. `@sparticuz/chromium` downloads but dies with
`libnspr4.so: cannot open shared object file`. So everything visual is
verified only structurally, and that is stated as a known gap rather than
hidden.

A harness is already written and waiting for a machine that can run it:
**`tests/browser/verify.mjs`** (Playwright, dev-only — `tests/browser/package.json`).

### Run it

```bash
python3 -m craft --port 8787 &                       # from the repo root
cd tests/browser && npm install && npx playwright install chromium
BASE=http://127.0.0.1:8787 node verify.mjs
```

For the sign-in gate, start a second server and point the harness at it:

```bash
python3 -m craft --port 8788 --auth-token s3cret &
BASE=http://127.0.0.1:8788 AUTH_TOKEN=s3cret node verify.mjs
```

It writes `tests/browser/artifacts/*.png` (one per view, one per viewport, one
per failure) and `artifacts/report.md`. Exit code 0 means every check passed.
The harness is a starting point, not a spec — extend it.

### What it checks today

1. Boot, 15 apps in the rail, brand renders once.
2. Every view renders (>120 chars, not the "failed to load" state) with no
   console errors, page errors, failed requests or 4xx/5xx responses.
3. SVGs contain marks with non-zero path length (i.e. a chart actually drew a
   line, not just an axis box).
4. Four viewports: 1440×900, 1280×800, 834×1112, 390×844 — no horizontal
   overflow, rail collapses below 860 px.
5. ⌘K / Ctrl-K palette opens and returns results for a query.
6. Drag a dashboard panel in edit mode, save, reload: `gridPos` changed and
   survived.
7. With `AUTH_TOKEN`: gate appears, wrong token rejected and not stored, right
   token mounts all 15 apps, credential persists, reload stays signed in.

### What to add

- **Pixel sanity.** Screenshots under 4 KB are usually blank — the harness
  already warns. Assert on a real image metric (non-uniform pixels in the
  chart region) rather than trusting file size.
- **Charts with data vs. empty state.** Point the server at a freshly seeded
  database (`CRAFT_DB=/tmp/x.db`) and at one with no data at all
  (`--no-seed`): every view must render a helpful empty state, never a broken
  chart or a spinner that never resolves.
- **Long/hostile strings.** A log line of 5 000 characters, a service name
  with emoji, a metric label with `<script>`. Nothing should overflow or
  execute (labels are rendered as text nodes — prove it).
- **Keyboard and focus.** Tab order through the rail, modals trapping focus,
  `Esc` closing, the `?`, `r`, `[` / `]` shortcuts.
- **Time range changes.** 5 m → 7 d on the overview: charts re-render, the
  range picker stays in sync with `hashchange`.
- **Console noise on navigation.** Walk all 15 routes twice; the second pass
  must not leak listeners, duplicate charts, or grow the DOM without bound
  (the app keeps a `{redraw, stop}` handle per mounted view — every view's
  `stop` must be called on unmount; a leak shows up as duplicated SVG ids).

### Definition of done for A

- `BASE=… node tests/browser/verify.mjs` exits 0 against a seeded server, an
  empty server, and an auth-enabled server.
- Every bug the run surfaced is either fixed (with a line in the PR's body
  describing the visual symptom and the cause) or written up in a new
  `docs/KNOWN-ISSUES.md` with a screenshot.
- `docs/ARCHITECTURE.md` §Testing and the README's "Verification" section stop
  saying "pixel-level rendering is unverified" and describe what is now
  actually checked, plus the exact command.
- Screenshots are **not** committed; `tests/browser/artifacts/` is git-ignored.

### Known suspects (start here)

- Chart clipping inside `.dash-panel` at tablet width; the SVG resize path
  uses `ResizeObserver`, which jsdom stubs out.
- `height: 100%` / `overflow: hidden` on `body` means one tall modal can hide
  its own footer on a short laptop screen.
- Drag on touch devices: `startDrag` is mouse-only (`mousedown`/`mousemove`),
  and is disabled entirely when the panel is narrow. Dragging on a tablet is
  probably impossible — decide whether that is intended and either support
  pointer events or document it.
- The sign-in gate re-runs `boot()` after login; make sure charts do not
  double-mount (the jsdom test only counted the first render).

---

## Workstream B — protocol gaps

In priority order. Each is independently shippable.

### B1. OTLP protobuf ingestion (highest value)

Today `/v1/metrics`, `/v1/logs`, `/v1/traces` accept JSON only
(`store.ingest_otlp_*`). Every default OpenTelemetry SDK ships an exporter that
speaks `application/x-protobuf` first, and `docs/COMPATIBILITY.md` currently
has to tell people to reconfigure with `encoding: json`.

**Goal.** Accept `Content-Type: application/x-protobuf` on those three paths
and decode it in pure Python.

**Shape of the work.**

- New module `craft/protobuf.py`: a wire-format decoder — varint, 64-bit,
  length-delimited, 32-bit; nested messages as slices; unknown fields skipped.
  Around 120 lines, no schema code generation.
- New module `craft/otlp_pb.py` (or a section of `store.py`): decode the three
  `Export*ServiceRequest` messages into **exactly the dict shape** the existing
  `ingest_otlp_*` functions already take. That is the whole trick: do not
  write a second ingestion path, write a decoder that feeds the first one.
  Then add one test per signal proving JSON and protobuf produce the same rows.
- Wire it in `Handler`/`route`: branch on the request `Content-Type` (also
  accept `application/x-protobuf; charset=…`), keep JSON as the fallback, and
  return `415` for an unknown content type. gRPC is **out of scope** — HTTP
  only.
- Field numbers come from `opentelemetry-proto` v1.x (`ExportMetricsServiceRequest.resource_metrics = 1`,
  `ResourceMetrics.resource = 1 / scope_metrics = 2`, `Metric.name = 1`,
  `Metric.gauge = 5 / sum = 7 / histogram = 9`, `NumberDataPoint.time_unix_nano = 3`,
  `as_double = 4 / as_int = 6`, `attributes = 7`, `LogRecord.time_unix_nano = 1`,
  `body = 5`, `severity_number = 2 / severity_text = 3`, `Span.trace_id = 1`,
  `span_id = 2`, `parent_span_id = 4`, `name = 5`, `kind = 6`,
  `start_time_unix_nano = 7`, `end_time_unix_nano = 8`, `status = 15`).
  **Verify every number against the .proto before you write the decoder** —
  a wrong tag decodes to silent garbage rather than an error.
- Fixtures: write a tiny encoder in the test file and round-trip through it,
  *and* commit one payload captured from a real exporter
  (`otel-cli span emit` / `otel-cli logs emit`, or the Go/JS SDK against a
  throwaway collector). The hand-rolled encoder alone cannot catch a wrong tag;
  a captured payload can. Keep captured fixtures small (< 8 KB each) and
  generate them with obviously fake data.

**Acceptance.**

```bash
python3 -m craft serve --port 8787 &
curl -s -X POST http://localhost:8787/v1/metrics \
     -H 'Content-Type: application/x-protobuf' --data-binary @tests/fixtures/otlp_metrics.bin
curl -s http://localhost:8787/api/prom/api/v1/query --data-urlencode 'query=demo_requests_total' | head
```

- JSON and protobuf payloads of the same content produce the same series,
  log rows and spans.
- A truncated/corrupt payload returns `400` with a readable message, never a
  traceback and never a 500.
- Unknown fields, empty repeated fields, and an empty body are all handled.
- `docs/COMPATIBILITY.md` §OpenTelemetry updated; the Collector app's copyable
  commands stop telling people to switch to JSON.
- Tests: decoder unit tests + three ingestion equivalence tests + one HTTP
  test that posts binary.

### B2. Prometheus recording rules

Alerting exists (`craft/alerting.py`: rules, pending → firing → resolved,
silences, receivers). Recording rules do not, and `docs/COMPATIBILITY.md` says
so.

**Goal.** Evaluate `record:` rules on an interval and store the results as
ordinary series, so dashboards and alerts can query them by name.

- Config: accept the Prometheus rule-file shape — `groups: [{name, interval,
  rules: [{record, expr, labels}]}]`. The fetch problem is YAML: there is no
  YAML parser in the standard library. Two acceptable answers:
  1. a ~150-line YAML *subset* parser covering block maps, block sequences,
     scalars, quoted strings and comments — enough for rule files, with a
     clear error for anything it does not understand; or
  2. namespace it as "JSON rule files" and require `curl` users to post JSON.
  Prefer (1) if you are confident in the tests; otherwise do (2) and say so
  loudly. Do **not** silently accept `.yaml` and misparse it.
- Storage: a new `recorded` table (`name`, `expr`, `labels`, `last_eval`,
  `enabled`) plus writing the evaluated series into the normal `series`/
  `samples` tables with an attribute distinguishing recorded output from
  scraped input. Schema change goes through `MIGRATIONS`.
- Evaluation: reuse the collector loop in `server.py` (it already ticks for
  alerts, synthetics and retention); evaluate every group on its `interval`,
  stamp results at the evaluation time, and never evaluate a rule against its
  own output (guard against `expr` referencing the recorded name).
- API: `GET /api/prom/api/v1/rules` returning the Prometheus shape
  `{"status":"success","data":{"groups":[{"name","interval","rules":[{"name","query","type":"recording","health","lastEvaluation","evaluationTime"}]}]}}`,
  and the same endpoint including alerting rules so a stock Prometheus client
  sees one consistent list.
- CLI + UI: `craft rules --type record` and a second tab in the Alerts app
  ("Recording rules") with its own table — same navigation language as the
  existing rules table.
- Retention/prune must cover recorded samples like any other sample.

**Acceptance.**

```bash
craft rules apply tests/fixtures/rules.yaml     # or POST /api/rules/groups
sleep 65
craft query 'job:http_requests:rate5m'
```

- The recorded series appears with the rule's labels and a fresh timestamp.
- `health` is `ok`/`err` with the last error message, and a broken `expr`
  fails one rule without taking down the group.
- Deleting a rule stops producing points within one interval.
- Tests: parser tests, evaluation tests (deterministic clock), API shape test,
  and a test that a rule referencing its own output does not run away.

### B3. LogQL metric queries over logs (stretch)

`rate({service="api"}[5m])`, `count_over_time`, `sum by (level) (…)` over log
streams rather than metrics. Everything needed is already in the log table;
this is an evaluator plus a chart. If you start it, keep the existing
selection/filter path untouched and add the metric form as a separate branch
in the query parser.

### Explicit non-goals

- gRPC (OTLP over HTTP only), remote read, federation, native histograms,
  `holt_winters`, OIDC/roles/multi-user auth. All of these are deliberate; do
  not start them in a branch that is meant to merge.

---

## How to deliver

- Branch from `arena/de07fa6c-rea-telemetry`, not `main`:
  `git checkout -b feat/<topic> origin/arena/de07fa6c-rea-telemetry`.
- One PR per workstream (A, B1, B2, B3), each with a body that states what is
  now true, what is still not, and the commands you ran. Screenshots for A go
  in the PR description, not in git.
- Open the PR against `arena/de07fa6c-rea-telemetry`.
- If something in this brief turns out to be wrong — a field number, an
  endpoint that does not behave as described, an assumption about the UI —
  fix the brief in the same PR. It is a document, not a contract.

## Where things live

| Path | What it is |
|---|---|
| `craft/db.py` | Schema, connection, migrations, `reset()` |
| `craft/store.py` | Ingest (Prometheus, Loki, OTLP, Sentry-shaped), metric writer, prune |
| `craft/promql.py` | PromQL engine, subqueries, `query_instant` / `query_range` |
| `craft/alerting.py` | Rules, alert state machine, silences, receivers, value formatting |
| `craft/query.py` | Log search and LogQL parsing |
| `craft/slo.py`, `craft/incidents.py`, `craft/synthetics.py`, `craft/loadtest.py` | The other apps |
| `craft/server.py` | Router, app catalog, collector loop, static serving |
| `craft/auth.py` | Token/basic gate, lockout, TLS context |
| `craft/cli.py` | 18 subcommands; `--token` for outbound commands |
| `craft/web/js/apps/*.js` | One module per view, 15 of them |
| `tests/` | `unittest`; `test_api.py` boots a real server, `test_auth.py` a real TLS socket |
| `docs/` | `ARCHITECTURE`, `API`, `CLI`, `COMPATIBILITY` — the last is the source of truth for gaps |
