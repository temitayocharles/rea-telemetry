# CLI reference

The same engine, without the browser. Useful for cron jobs, CI gates, SSH sessions, and for
feeding an agent or script that only understands text and JSON.

```
telemetrycraft <command> [options]        # after `pip install .`
craft <command> [options]                 # same binary, shorter name
python3 -m craft <command> [options]      # from a checkout, no install
```

Global option on every command:

```
--db PATH        SQLite file to use (default: data/telemetry.db, or $CRAFT_DB)
--token TOKEN    bearer token for commands that talk to a server (or $CRAFT_TOKEN);
                 accepted before or after the subcommand
```

Everything is offline and local except `write`, `send-log` (which POST to a running server),
`load` (which sends real HTTP to the target you name) and `serve`.

Exit code is `0` on success. Bad input, an unknown trace or an unreachable server exits non-zero
with the message on stderr. `--json` output is safe to pipe into `jq`.

---

### `serve`

```bash
craft serve [--port 8787] [--host 0.0.0.0] [--no-seed]
            [--auth-token TOKEN | --auth-user USER --auth-password PASS] [--no-auth]
            [--tls-cert cert.pem --tls-key key.pem [--tls-client-ca ca.pem]]
```

Starts the HTTP API, the console, and the collector loop (alert evaluation, synthetic checks,
retention). Seeds the demo environment on first boot unless `--no-seed`.

**Auth.** `--auth-token` requires a bearer token, `--auth-user`/`--auth-password` requires HTTP
basic auth (both may be set). The environment equivalents are `CRAFT_AUTH_TOKEN`,
`CRAFT_AUTH_USER`, `CRAFT_AUTH_PASSWORD`; flags win. `--no-auth` disables whatever the environment
enabled. There is no user database: one token or one user/password pair, compared in constant time,
which is the right shape for a tool that runs on your laptop or one box. Ten failures a minute
from a client gets `429`; a success clears the counter. The browser asks for the credential once at
`/api/auth/login` and keeps it in `localStorage` for that origin. `/api/health`,
`/api/status/health`, `/api/auth/status` and `/api/auth/login` stay open.

```bash
export CRAFT_AUTH_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
craft serve --port 8787
curl -H "Authorization: Bearer $CRAFT_AUTH_TOKEN" http://localhost:8787/api/overview
```

**TLS.** `--tls-cert` plus `--tls-key` (PEM) serve HTTPS directly — no reverse proxy required, and
no certificate is generated for you. `--tls-client-ca` additionally demands a client certificate
signed by that CA (mTLS). TLS 1.2 is the floor. Environment: `CRAFT_TLS_CERT`, `CRAFT_TLS_KEY`,
`CRAFT_TLS_CLIENT_CA`.

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -keyout key.pem -out cert.pem
craft serve --tls-cert cert.pem --tls-key key.pem --auth-token "$CRAFT_AUTH_TOKEN"
# Server prints: TelemetryCraft 1.0.0 listening on https://0.0.0.0:8787
```

### `seed`

```bash
craft seed [--force] [--self-url http://127.0.0.1:8787]
```

Generates seven days of correlated telemetry for six services: metrics, logs, traces, error
issues, alerts, SLOs, checks and incidents. `--force` wipes and regenerates.

### `status`

```bash
craft status
```

```
TelemetryCraft  /home/you/rea-telemetry/data/telemetry.db
  series        270
  samples       643,814  (62,480 in the last hour)
  logs          4,200
  spans         4,068
  alerts        5 firing
  incidents     1 open
  disk          41.9 MB (retention 30d)
```

---

### `query`

```bash
craft query '<PromQL>' [--range 1h] [--step 60] [--json]
```

Instant query by default, returning one row per series. With `--range`, prints the last value and
the number of points per series; `--json` emits the raw Prometheus-shaped response.

```bash
craft query 'sum(rate(http_requests_total[5m])) by (service)'
craft query 'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))'
craft query 'predict_linear(node_filesystem_avail_bytes[1h], 4*3600) < 0' --json
craft query 'sum(rate(http_requests_total[5m]))' --range 6h --step 300
```

### `metrics` and `labels`

```bash
craft metrics                 # metric name, series count, type
craft labels                  # every label name
craft labels service          # values for one label
```

### `logs`

```bash
craft logs [QUERY] [--since 1h] [--limit 40] [--json]
```

QUERY is a LogQL stream selector with optional filters; default `{service=~".+"}`.

```bash
craft logs '{service="checkout-service"} |= "timeout"' --since 2h
craft logs '{level="error"} !~ "expected"' --since 15m --limit 200
craft logs '{service="payment-service"}' --json | jq '.entries | length'
```

### `traces`

```bash
craft traces [TRACE_ID] [--service S] [--status error|ok] [--min-duration MS]
             [--since 1h] [--limit 15] [--json]
```

Without an id: search, slowest first. With an id: print the waterfall as a tree with durations and
status, plus span attributes.

```bash
craft traces --service payment-service --min-duration 500 --since 30m
craft traces 4f2c9a1b7e3d4c5f8a9b0c1d2e3f4a5b
```

### `alerts`

```bash
craft alerts [--evaluate] [--ack ID --by NAME]
```

Lists alert instances with state, severity, name, labels and value. Values are rendered in the
unit each rule declares (`bytes`, `ratio`, `seconds`, …), and a projection that has already gone
negative says so: `−11.5 GiB (projected past zero)`. `--json` includes the raw float. `--evaluate` runs every rule now
(the collector does this every 15 s anyway) and prints the transitions.

### `rules`

```bash
craft rules
```

Rule name, expression, `for`, severity, and how many instances are firing.

### `incidents`

```bash
craft incidents [--declare TITLE --severity SEV2 --commander NAME] [--resolve INC-0004]
```

`--declare` opens an incident and returns its id; `--resolve` closes one by id.

### `slos`

```bash
craft slos
```

Target, achieved availability, budget remaining, burn rates and status per SLO.

### `checks`

```bash
craft checks [--run ID]
```

Lists synthetic checks with uptime and p95 latency; `--run` executes one immediately.

---

### `write`

```bash
craft write METRIC VALUE [--label k=v ...] [--url http://127.0.0.1:8787]
```

Sends a single sample to a running server. Repeat `--label`.

```bash
craft write deploy_started 1 --label service=checkout --label env=prod
```

### `send-log`

```bash
craft send-log [--service S] [--level L] [--body TEXT] [--stdin] [--url URL]
```

`--stdin` reads lines and sends them as one batch — handy for tailing a file:

```bash
tail -f /var/log/app.log | craft send-log --service billing --level info --stdin
```

### `load`

```bash
craft load --target URL [--name NAME] [--vus 5] [--duration 20] [--method GET]
```

Runs a real, bounded HTTP load test (max 20 virtual users, 60 s) and prints per-second p50/p95/p99
latency, RPS and error rate. Results are stored, so they appear in the Load app too.

```bash
craft load --target http://localhost:8787/api/health --vus 10 --duration 30
```

### `export`

```bash
craft export [--out snapshot.json]
```

Writes one JSON document with dashboards, rules, SLOs, checks, incidents and metadata — enough to
rebuild a configuration elsewhere. Prints to stdout without `--out`.

### `prune`

```bash
craft prune [--days 30]
```

Applies retention now instead of waiting for the collector. Prints deleted row counts.

---

## Scripting examples

Fail a deploy if the error budget is nearly gone:

```bash
budget=$(craft slos --json | jq -r '.slos[] | select(.service=="checkout-service") | .budget_remaining_pct')
[ "${budget%.*}" -lt 10 ] && echo "budget below 10%" && exit 1
```

Snapshot the last hour of errors before a release:

```bash
curl -s 'localhost:8787/api/logs' \
  --data-urlencode 'query={level="error"}' \
  --data-urlencode "start=$(( $(date +%s) - 3600 ))" \
  --data-urlencode "end=$(date +%s)" --data-urlencode 'limit=1000' > pre-release-errors.json
```

Promote a firing alert to an incident and page:

```bash
id=$(craft alerts --json | jq -r '.alerts[] | select(.state=="firing") | .id' | head -1)
craft incidents --declare "Checkout failing" --severity SEV1 --commander oncall
```
