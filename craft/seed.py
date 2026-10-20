"""Generate a realistic, deterministic demo environment.

Everything below is synthetic but behaves like production: 7 days of time
series at two resolutions, correlated logs and traces, an active incident that
actually trips the alert rules, synthetics history and an error budget.
"""
from __future__ import annotations

import json
import math
import os
import random

from . import alerting, dashboards, db, store

SEED_VERSION = 3

SERVICES = [
    {"name": "api-gateway", "display": "API Gateway", "team": "Platform", "tier": 1, "rps": 420, "p50": 0.012, "sigma": 0.85, "routes": ["/v1/checkout", "/v1/search", "/v1/cart", "/v1/auth", "/v1/orders", "/healthz"]},
    {"name": "checkout-service", "display": "Checkout", "team": "Payments", "tier": 1, "rps": 96, "p50": 0.085, "sigma": 1.05, "routes": ["/checkout/submit", "/checkout/quote", "/checkout/confirm"]},
    {"name": "payment-service", "display": "Payments", "team": "Payments", "tier": 1, "rps": 61, "p50": 0.14, "sigma": 1.15, "routes": ["/pay/authorize", "/pay/capture", "/pay/refund"]},
    {"name": "cart-service", "display": "Cart", "team": "Commerce", "tier": 2, "rps": 128, "p50": 0.021, "sigma": 0.8, "routes": ["/cart/add", "/cart/view", "/cart/clear"]},
    {"name": "search-service", "display": "Search", "team": "Commerce", "tier": 2, "rps": 240, "p50": 0.038, "sigma": 0.95, "routes": ["/search/query", "/search/suggest"]},
    {"name": "auth-service", "display": "Auth", "team": "Platform", "tier": 1, "rps": 305, "p50": 0.009, "sigma": 0.7, "routes": ["/auth/token", "/auth/introspect", "/auth/login"]},
    {"name": "inventory-service", "display": "Inventory", "team": "Commerce", "tier": 2, "rps": 82, "p50": 0.026, "sigma": 0.9, "routes": ["/inventory/reserve", "/inventory/release"]},
    {"name": "notification-service", "display": "Notifications", "team": "Growth", "tier": 3, "rps": 41, "p50": 0.055, "sigma": 1.0, "routes": ["/notify/email", "/notify/push"]},
]

HOSTS = [
    ("prod-node-01", "10.42.8.11", 32),
    ("prod-node-02", "10.42.8.12", 32),
    ("prod-node-03", "10.42.8.13", 64),
    ("prod-node-04", "10.42.8.14", 32),
    ("prod-node-05", "10.42.8.15", 64),
    ("prod-node-06", "10.42.8.16", 32),
]

NAMESPACES = [("production", "platform"), ("production", "payments"), ("production", "commerce"), ("staging", "platform")]

LE = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]


def le_label(value: float) -> str:
    """Match Prometheus label formatting: le="1" not le="1.0"."""
    return "+Inf" if value == float("inf") else (str(int(value)) if float(value).is_integer() else repr(float(value)))


def _lognormal_cdf(x, mu, sigma):
    if x <= 0:
        return 0.0
    return 0.5 * (1 + math.erf((math.log(x) - mu) / (sigma * math.sqrt(2))))


def _mu(p50, sigma):
    return math.log(p50)


def incident_factor(service: str, ts: int, now: int) -> tuple[float, float]:
    """Return (error_rate, latency_multiplier) for a point in time."""
    err, mult = 0.0015, 1.0
    since = now - ts
    if service in ("checkout-service", "api-gateway") and 0 <= since <= 1800:
        progress = 1 - since / 1800
        err = 0.0015 + 0.062 * (0.4 + 0.6 * progress)
        mult = 1.0 + 1.3 * (0.5 + 0.5 * progress)
    if service == "search-service" and 3 * 3600 <= since <= 3 * 3600 + 1500:
        mult = 2.4
        err = 0.011
    if service == "notification-service" and 9 * 3600 <= since <= 9 * 3600 + 3600:
        err = 0.02
    return err, mult


def seed(self_url: str = "http://127.0.0.1:8787", force: bool = False) -> dict:
    conn = db.connect()
    if not force and db.get_meta("seed_version") == SEED_VERSION:
        return {"seeded": False, "reason": "already seeded"}
    if force:
        db.reset()
        conn = db.connect()
        from .promql import bump_series_version

        bump_series_version()

    random.seed(42)
    now = db.now()
    now -= now % 15
    t_hist = now - 7 * 86400
    t_live = now - 7200
    grid = list(range(t_hist, t_live, 300)) + list(range(t_live, now + 1, 15))

    counts = {"samples": 0, "logs": 0, "spans": 0, "issues": 0, "checks": 0}

    services = SERVICES
    counters: dict[tuple, float] = {}

    def add_metric(w, metric, labels, ts, value, stype=None):
        w.add(metric, labels, ts, value, stype)
        counts["samples"] += 1

    w = store.MetricWriter(conn)
    for svc in services:
        name = svc["name"]
        mu, sigma = _mu(svc["p50"], svc["sigma"]), svc["sigma"]
        counter_state = {s: 0.0 for s in ("200", "404", "500")}
        bucket_state = {le: 0.0 for le in LE + [float("inf")]}
        sum_state = 0.0
        count_state = 0.0
        mem_base = 180 + 90 * random.random() + (60 if svc["tier"] == 1 else 0)
        for idx, ts in enumerate(grid):
            hour = (ts % 86400) / 3600
            diurnal = 1 + 0.38 * math.sin((hour - 9) / 24 * 2 * math.pi) + 0.08 * math.sin((hour - 2) / 12 * 2 * math.pi)
            weekend = 0.82 if ((ts // 86400) + 4) % 7 in (5, 6) else 1.0
            noise = 1 + 0.07 * (random.random() - 0.5)
            err_rate, mult = incident_factor(name, ts, now)
            rps = max(svc["rps"] * diurnal * weekend * noise, 1.0)
            dt = 15 if idx and ts >= t_live else 300
            labels = {"service": name, "namespace": "production", "job": name}
            # counters
            for status, share in (("200", 1 - err_rate - 0.004), ("404", 0.004), ("500", err_rate)):
                counter_state[status] += rps * dt * share
                add_metric(w, "http_requests_total", {**labels, "status": status, "method": "GET", "route": svc["routes"][0]}, ts, counter_state[status], "counter")
            # histogram
            lat_scale = svc["p50"] * mult
            mu_i = math.log(lat_scale)
            for le in LE + [float("inf")]:
                frac = 1.0 if le == float("inf") else _lognormal_cdf(le, mu_i, sigma)
                bucket_state[le] += rps * dt * frac
                add_metric(w, "http_request_duration_seconds_bucket", {**labels, "le": le_label(le)}, ts, bucket_state[le], "histogram")
            mean_lat = lat_scale * math.exp(sigma * sigma / 2)
            sum_state += rps * dt * mean_lat
            count_state += rps * dt
            add_metric(w, "http_request_duration_seconds_sum", labels, ts, sum_state, "histogram")
            add_metric(w, "http_request_duration_seconds_count", labels, ts, count_state, "histogram")
            # gauges
            cpu = 0.18 + 0.42 * (rps / max(svc["rps"], 1)) * 0.55 + 0.03 * random.random()
            add_metric(w, "app_cpu_usage_ratio", labels, ts, min(cpu, 0.99))
            add_metric(w, "app_memory_working_set_bytes", labels, ts, (mem_base + 26 * math.sin(ts / 7200) + 9 * random.random()) * 1024 * 1024)
            add_metric(w, "http_requests_in_flight", labels, ts, round(rps * (0.006 + 0.002 * random.random()) * (1.8 if name == "checkout-service" and ts > now - 1800 else 1), 1))
            add_metric(w, "app_threads", labels, ts, 24 + rps / 40 + (12 if ts > now - 1800 and name == "checkout-service" else 0))
            if svc["tier"] <= 2:
                backlog = 40 + 30 * random.random() + (260 if name == "payment-service" and 2 * 3600 < now - ts < 2.5 * 3600 else 0)
                add_metric(w, "queue_messages_ready", {"queue": f"{name}-events", "service": name}, ts, round(backlog))
                add_metric(w, "queue_messages_inflight", {"queue": f"{name}-events", "service": name}, ts, round(backlog * 0.18))
    w.flush()

    # ---- hosts ----------------------------------------------------------
    for host, ip, cores in HOSTS:
        user_total = 0.0
        idle_total = 0.0
        for ts in grid:
            labels = {"instance": f"{host}:9100", "job": "node-exporter"}
            dt = 15 if ts >= t_live else 300
            busy = 0.22 + 0.12 * math.sin(ts / 5000 + cores) + 0.06 * random.random()
            if host == "prod-node-04":
                busy += 0.18
            busy_seconds = cores * dt * busy
            user_total += busy_seconds
            idle_total += cores * dt - busy_seconds
            add_metric(w, "node_cpu_seconds_total", {**labels, "mode": "user"}, ts, user_total, "counter")
            add_metric(w, "node_cpu_seconds_total", {**labels, "mode": "idle"}, ts, idle_total, "counter")
            total_mem = 64 * 1024 ** 3 if cores == 64 else 32 * 1024 ** 3
            used = total_mem * (0.58 + 0.1 * math.sin(ts / 9000) + 0.04 * random.random())
            add_metric(w, "node_memory_MemTotal_bytes", labels, ts, total_mem)
            add_metric(w, "node_memory_MemAvailable_bytes", labels, ts, total_mem - used)
            disk_total = 500 * 1024 ** 3
            if host == "prod-node-04":
                consumed = 0.83 + 0.09 * (1 - (now - ts) / (7 * 86400))
                if ts > now - 7200:  # log flood fills the disk while we watch
                    consumed += 0.078 * ((ts - (now - 7200)) / 7200)
            else:
                consumed = 0.41 + 0.05 * math.sin(ts / 40000) + 0.01 * random.random()
            add_metric(w, "node_filesystem_free_bytes", {**labels, "mountpoint": "/", "device": "/dev/nvme0n1p1"}, ts, disk_total * (1 - consumed))
            add_metric(w, "node_filesystem_size_bytes", {**labels, "mountpoint": "/", "device": "/dev/nvme0n1p1"}, ts, disk_total)
            add_metric(w, "node_load1", labels, ts, round((2.2 + 3.4 * busy + 0.4 * random.random()) * (cores / 32), 2))
            add_metric(w, "up", {"job": "node-exporter", "instance": f"{host}:9100"}, ts, 1)
    # scrape health & up
    for job in ("api-gateway", "checkout-service", "payment-service", "search-service"):
        for ts in grid[::4]:
            add_metric(w, "up", {"job": job, "instance": f"{job}.production.svc:8080"}, ts, 1)
            add_metric(w, "scrape_duration_seconds", {"job": job, "instance": f"{job}.production.svc:8080"}, ts, round(0.012 + 0.01 * random.random(), 4))
    w.flush()

    # ---- kubernetes -----------------------------------------------------
    restarts = {s["name"]: 0.0 for s in services}
    for svc in services:
        dep = svc["name"]
        for ts in grid:
            dt = 15 if ts >= t_live else 300
            labels = {"deployment": dep, "namespace": "production", "job": "kube-state-metrics"}
            add_metric(w, "kube_deployment_status_replicas_available", labels, ts, 3 if svc["tier"] == 1 else 2)
            available = 3 if svc["tier"] == 1 else 2
            unavailable = 1 if dep == "payment-service" and ts > now - 2.5 * 3600 and ts < now - 2.1 * 3600 else 0
            add_metric(w, "kube_deployment_status_replicas_unavailable", labels, ts, unavailable)
            if dep == "payment-service" and ts > now - 2.5 * 3600:
                restarts[dep] += random.choice([0, 0, 0, 1]) if random.random() < 0.08 else 0
            add_metric(w, "kube_pod_container_status_restarts_total", {**labels, "pod": f"{dep}-{hash(dep) % 7}x9k2"}, ts, restarts[dep], "counter")
    for ts in grid[::12]:
        add_metric(w, "kube_node_info", {"node": HOSTS[0][0], "namespace": "production"}, ts, 1)
        add_metric(w, "kube_node_info", {"node": HOSTS[1][0], "namespace": "production"}, ts, 1)
        add_metric(w, "kube_node_info", {"node": HOSTS[2][0], "namespace": "production"}, ts, 1)
        add_metric(w, "kube_node_info", {"node": HOSTS[3][0], "namespace": "production"}, ts, 1)

    # ---- database / redis ----------------------------------------------
    for ts in grid:
        add_metric(w, "pg_stat_activity_count", {"instance": "postgres-primary:9187", "database": "shop", "job": "postgres"}, ts, 92 + 18 * math.sin(ts / 3600) + 34 * (1 if now - ts < 1800 else 0))
        add_metric(w, "pg_settings_max_connections", {"instance": "postgres-primary:9187", "job": "postgres"}, ts, 200)
        add_metric(w, "pg_stat_database_xact_commit_total", {"instance": "postgres-primary:9187", "database": "shop", "job": "postgres"}, ts, 1400 + (ts - t_hist) * 118.0, "counter")
        add_metric(w, "pg_stat_database_numbackends", {"instance": "postgres-primary:9187", "database": "shop"}, ts, 61)
        add_metric(w, "redis_memory_used_bytes", {"instance": "redis-cache:9121", "job": "redis"}, ts, 1.9e9 + 2.2e8 * math.sin(ts / 8000))
        add_metric(w, "redis_keyspace_hits_total", {"instance": "redis-cache:9121", "job": "redis"}, ts, (ts - t_hist) * 3400.0, "counter")
        add_metric(w, "redis_keyspace_misses_total", {"instance": "redis-cache:9121", "job": "redis"}, ts, (ts - t_hist) * 210.0, "counter")
        add_metric(w, "redis_connected_clients", {"instance": "redis-cache:9121", "job": "redis"}, ts, 74)

    # ---- business -------------------------------------------------------
    orders = revenue = signups = 0.0
    for ts in grid:
        dt = 15 if ts >= t_live else 300
        hour = (ts % 86400) / 3600
        diurnal = 1 + 0.45 * math.sin((hour - 9) / 24 * 2 * math.pi)
        order_rate = 2.6 * diurnal
        success_share = 0.972 if now - ts < 1800 else 0.994
        orders += order_rate * dt
        revenue += order_rate * dt * (78 + 40 * random.random())
        signups += 0.7 * dt * diurnal
        add_metric(w, "checkout_orders_total", {"status": "success"}, ts, orders * success_share, "counter")
        add_metric(w, "checkout_orders_total", {"status": "failed"}, ts, orders * (1 - success_share), "counter")
        add_metric(w, "checkout_revenue_dollars_total", {"currency": "USD"}, ts, revenue, "counter")
        add_metric(w, "signups_total", {"plan": "self_serve"}, ts, signups, "counter")
    w.flush()

    # ---- service catalog & config --------------------------------------
    conn.executemany(
        "INSERT OR REPLACE INTO services(name, display, team, tier, repo, oncall, description) VALUES(?,?,?,?,?,?,?)",
        [
            (s["name"], s["display"], s["team"], s["tier"], f"github.com/acme/{s['name']}", f"{s['team'].lower()}-primary", f"{s['display']} service")
            for s in services
        ],
    )
    alerting.seed_defaults()
    dashboards.install_defaults(force=True)
    _seed_slos(services)
    _seed_pipelines()
    _seed_logs(services, now, counts)
    _seed_spans(services, now, counts)
    _seed_issues(now, counts)
    _seed_checks(self_url, now, counts)
    _seed_incidents(now)
    _seed_loadtest(now)
    _seed_alerts(now)
    db.set_meta("seed_version", SEED_VERSION)
    db.set_meta("seed_at", now)
    db.set_meta("self_url", self_url)
    db.set_meta("org", {"name": "Acme Commerce", "plan": "self-hosted", "region": "us-east-1"})
    db.set_meta(
        "collector",
        {
            "receivers": ["otlp", "prometheus", "loki", "jaeger"],
            "processors": ["memory_limiter", "batch", "attributes/scrub"],
            "exporters": ["telemetrycraft"],
            "retention_days": 30,
        },
    )
    return {"seeded": True, **counts, "series": conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]}


def _seed_slos(services):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM slos").fetchone()[0]:
        return
    rows = [
        ("Checkout availability", "checkout-service", 0.999, 30,
         'sum(rate(http_requests_total{service="checkout-service",status!~"5.."}[5m]))',
         'sum(rate(http_requests_total{service="checkout-service"}[5m]))',
         "Checkout must succeed 99.9% of the time over a rolling 30 days."),
        ("Checkout latency", "checkout-service", 0.99, 30,
         'sum(rate(http_request_duration_seconds_bucket{service="checkout-service",le="1"}[5m]))',
         'sum(rate(http_request_duration_seconds_count{service="checkout-service"}[5m]))',
         "99% of checkout requests finish under one second."),
        ("API gateway availability", "api-gateway", 0.9995, 30,
         'sum(rate(http_requests_total{service="api-gateway",status!~"5.."}[5m]))',
         'sum(rate(http_requests_total{service="api-gateway"}[5m]))',
         "Edge availability target for the public API."),
        ("Search availability", "search-service", 0.995, 30,
         'sum(rate(http_requests_total{service="search-service",status!~"5.."}[5m]))',
         'sum(rate(http_requests_total{service="search-service"}[5m]))',
         "Search availability target."),
        ("Payments availability", "payment-service", 0.9999, 30,
         'sum(rate(http_requests_total{service="payment-service",status!~"5.."}[5m]))',
         'sum(rate(http_requests_total{service="payment-service"}[5m]))',
         "Payments is the tightest budget in the platform."),
    ]
    conn.executemany(
        "INSERT INTO slos(name, service, objective, window_days, sli_expr, total_expr, description) VALUES(?,?,?,?,?,?,?)", rows
    )


def _seed_pipelines():
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]:
        return
    conn.executemany(
        "INSERT INTO pipelines(name, signal, receivers, processors, exporters, enabled) VALUES(?,?,?,?,?,?)",
        [
            ("metrics/otlp", "metrics", json.dumps(["otlp", "prometheus"]), json.dumps(["memory_limiter", "batch"]), json.dumps(["telemetrycraft"]), 1),
            ("logs/otlp", "logs", json.dumps(["otlp", "loki"]), json.dumps(["memory_limiter", "attributes/scrub", "batch"]), json.dumps(["telemetrycraft"]), 1),
            ("traces/otlp", "traces", json.dumps(["otlp", "jaeger"]), json.dumps(["memory_limiter", "tail_sampling"]), json.dumps(["telemetrycraft"]), 1),
        ],
    )


LOG_TEMPLATES = {
    "error": [
        "upstream request failed: POST https://payments.internal/pay/authorize returned 503 after {ms}ms",
        "timeout calling inventory-service: context deadline exceeded after {ms}ms",
        "panic recovered in order worker: nil pointer dereference at checkout/worker.go:{line}",
        "database error: pq: too many connections (max 200)",
        "payment authorization declined for order {order}: gateway_error_code=issuer_timeout",
        "kafka publish failed for topic checkout.events: NOT_LEADER_FOR_PARTITION",
    ],
    "warn": [
        "retrying request after 429 from ledger API (attempt 2/5)",
        "slow query detected: SELECT * FROM orders WHERE customer_id=$1 took {ms}ms",
        "cache miss ratio above threshold for cart-service: 34%",
        "connection pool at 87% utilisation, consider raising max_connections",
        "synthetic check latency {ms}ms exceeded soft threshold",
    ],
    "debug": [
        "cache lookup cart:{user} hit=true ttl=284s",
        "span exported to collector in {ms}ms",
        "pool stats idle=4 busy=2 waiting=0",
        "feature flags refreshed (revision {hits})",
    ],
    "info": [
        "request completed method=POST route={route} status={status} duration={ms}ms",
        "order {order} confirmed in {ms}ms",
        "issued access token for user u_{user} (ttl=900s)",
        "search query served from warm index in {ms}ms hits={hits}",
        "cart snapshot persisted for session s_{user}",
        "inventory reserved {hits} units for sku SKU-{user}",
        "published {hits} notifications to email worker",
    ],
}

STACK_TEMPLATES = [
    [{"module": "checkout/handler.go", "function": "SubmitOrder", "line": 142, "source": "charge, err := payments.Authorize(ctx, req)"},
     {"module": "payments/client.go", "function": "Authorize", "line": 88, "source": "resp, err := c.do(ctx, req)"},
     {"module": "net/http/client.go", "function": "Do", "line": 702, "source": "return c.send(req, deadline)"}],
    [{"module": "inventory/reserve.go", "function": "Reserve", "line": 61, "source": "rows, err := tx.QueryContext(ctx, q, sku)"},
     {"module": "database/sql/sql.go", "function": "QueryContext", "line": 3951, "source": "return db.query(ctx, query, args, cachedOrNewConn)"},
     {"module": "github.com/lib/pq/conn.go", "function": "query", "line": 1523, "source": "return cn.simpleQuery(query)"}],
    [{"module": "search/index.ts", "function": "runQuery", "line": 217, "source": "const res = await client.search({ index, body })"},
     {"module": "node_modules/@elastic/transport/lib/Transport.js", "function": "request", "line": 284, "source": "return await this.connection.request(params)"},
     {"module": "node:internal/process/task_queues", "function": "processTicksAndRejections", "line": 95, "source": ""}],
]


def _seed_logs(services, now, counts):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]:
        return
    rows = []
    routes = {s["name"]: s["routes"] for s in services}
    weights = [(s["name"], s["tier"]) for s in services]
    pool = []
    for name, tier in weights:
        pool += [name] * (5 if tier == 1 else 3 if tier == 2 else 2)
    for i in range(4200):
        svc = random.choice(pool)
        recent = random.random() < 0.45
        ts = now - int(random.random() * 1800) if recent else now - int(random.random() * 86400)
        r = random.random()
        if svc == "checkout-service" and now - ts < 1800:
            level = random.choices(["error", "warn", "info"], [0.34, 0.24, 0.42])[0]
        elif r < 0.055:
            level = "error"
        elif r < 0.15:
            level = "warn"
        elif r < 0.18:
            level = "debug"
        else:
            level = "info"
        tpl = random.choice(LOG_TEMPLATES[level])
        body = tpl.format(
            ms=random.choice([12, 34, 57, 120, 480, 1240, 2900, 6200]),
            line=random.randint(40, 400),
            order=f"ord_{random.randint(100000, 999999)}",
            user=random.randint(1000, 9999),
            route=random.choice(routes[svc]),
            status=random.choice([200, 200, 200, 201, 404, 500]),
            hits=random.randint(1, 240),
        )
        trace_id = "%032x" % random.getrandbits(128)
        labels = {"service_name": svc, "namespace": "production", "cluster": "prod-use1", "pod": f"{svc}-{random.randint(10000,99999)}"}
        attrs = {"http.route": random.choice(routes[svc]), "http.method": "GET" if random.random() < 0.7 else "POST"}
        if level in ("error", "warn"):
            attrs["error.kind"] = "UpstreamError" if "upstream" in body else "Timeout"
        rows.append((ts, svc, level, body, json.dumps(labels), json.dumps(attrs), trace_id if random.random() < 0.75 else None, None))
    conn.executemany("INSERT INTO logs(ts, service, level, body, labels, attrs, trace_id, span_id) VALUES(?,?,?,?,?,?,?,?)", rows)
    counts["logs"] = len(rows)


def _seed_spans(services, now, counts):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]:
        return
    rows = []
    methods = ["GET", "POST"]
    for i in range(900):
        recent = random.random() < 0.55
        start_s = now - random.randint(0, 1800) if recent else now - random.randint(1800, 6 * 3600)
        trace_id = "%032x" % random.getrandbits(128)
        bad = random.random() < (0.30 if recent else 0.05)
        root_dur = random.choice([38, 55, 74, 96, 130, 180, 240, 420, 1200]) * 1000
        route = "/v1/checkout" if bad and recent else random.choice(["/v1/search", "/v1/cart", "/v1/checkout", "/v1/auth", "/v1/orders"])
        root_id = "%016x" % random.getrandbits(64)
        root_start = start_s * 1_000_000 + random.randint(0, 999_999)
        rows.append((trace_id, root_id, None, "api-gateway", f"HTTP {random.choice(methods)} {route}", root_start, root_dur,
                     "error" if bad else "ok", "server",
                     json.dumps({"http.method": "POST" if route == "/v1/checkout" else "GET", "http.route": route, "http.status_code": 503 if bad else 200, "net.peer.ip": f"10.42.{random.randint(1,9)}.{random.randint(1,250)}"}),
                     json.dumps([])))
        children = []
        if route in ("/v1/checkout", "/v1/orders"):
            children = [("checkout-service", "POST /checkout/submit", 0.62, 1),
                        ("cart-service", "GET /cart/view", 0.12, 0),
                        ("inventory-service", "POST /inventory/reserve", 0.18, 0),
                        ("payment-service", "POST /pay/authorize", 0.38, 2)]
        elif route == "/v1/search":
            children = [("search-service", "GET /search/query", 0.72, 0)]
        elif route == "/v1/cart":
            children = [("cart-service", "POST /cart/add", 0.66, 0), ("inventory-service", "POST /inventory/reserve", 0.2, 0)]
        else:
            children = [("auth-service", "POST /auth/introspect", 0.55, 0)]
        for svc_name, op, frac, depth in children:
            dur = max(int(root_dur * frac * random.uniform(0.75, 1.05)), 2000)
            offset = int((root_dur - dur) * random.uniform(0.02, 0.3)) if depth == 0 else int(root_dur * 0.1)
            sid = "%016x" % random.getrandbits(64)
            err = bad and svc_name in ("payment-service", "checkout-service")
            rows.append((trace_id, sid, root_id, svc_name, op, root_start + offset, dur,
                         "error" if err else "ok", "client" if depth == 0 else "server",
                         json.dumps({"rpc.system": "http", "http.status_code": 503 if err else 200, "peer.service": svc_name}), json.dumps([])))
            if depth >= 1 and random.random() < 0.8:
                db_name = "postgresql" if svc_name == "checkout-service" else "redis"
                stmt = "INSERT INTO payments (order_id, amount, status) VALUES ($1, $2, $3)" if db_name == "postgresql" else "GET cart:{id}"
                rows.append((trace_id, "%016x" % random.getrandbits(64), sid, "postgres-primary" if db_name == "postgresql" else "redis-cache",
                             stmt[:48], root_start + offset + random.randint(1000, 8000), int(dur * random.uniform(0.15, 0.55)),
                             "error" if err and db_name == "postgresql" else "ok", "client",
                             json.dumps({"db.system": db_name, "db.statement": stmt}), json.dumps([])))
    # one long, fully-instrumented trace worth opening
    trace_id = "a" * 32
    t0 = (now - 900) * 1_000_000
    root = "b" * 16
    rows.append((trace_id, root, None, "api-gateway", "POST /v1/checkout", t0, 3_842_000, "error", "server",
                 json.dumps({"http.method": "POST", "http.route": "/v1/checkout", "http.status_code": 503, "user.id": "u_88213", "cart.items": 3}), json.dumps([])))
    spans = [
        ("checkout-service", "POST /checkout/submit", 6000, 3_100_000, "error", "client"),
        ("auth-service", "POST /auth/introspect", 42000, 9_000, "ok", "client"),
        ("cart-service", "GET /cart/view", 62000, 21_000, "ok", "client"),
        ("inventory-service", "POST /inventory/reserve", 95000, 84_000, "ok", "client"),
        ("payment-service", "POST /pay/authorize", 210000, 2_640_000, "error", "client"),
    ]
    prev = root
    for i, (svc, op, off, dur, status, kind) in enumerate(spans):
        sid = f"c{i}" + "0" * 14
        rows.append((trace_id, sid, prev if i < 2 else root, svc, op, t0 + off, dur, status, kind,
                     json.dumps({"http.status_code": 503 if status == "error" else 200, "peer.service": svc}), json.dumps([])))
        if svc == "payment-service":
            rows.append((trace_id, "d" + "0" * 15, sid, "postgres-primary", "INSERT INTO payments (order_id, amount, status) VALUES ($1, $2, $3)", t0 + off + 12000, 2_400_000, "error", "client",
                         json.dumps({"db.system": "postgresql", "db.statement": "INSERT INTO payments (order_id, amount, status) VALUES ($1, $2, $3)", "error": "pq: deadlock detected"}), json.dumps([{"name": "exception", "ts": int((t0 + off + 2_300_000) / 1000), "attrs": {"exception.type": "DeadlockDetected", "exception.message": "pq: deadlock detected"}}])))
            rows.append((trace_id, "e" + "0" * 15, sid, "payment-service", "gateway.authorize", t0 + off + 14000, 2_610_000, "error", "client",
                         json.dumps({"http.status_code": 504, "peer.service": "stripe-gateway"}), json.dumps([])))
    conn.executemany(
        "INSERT OR REPLACE INTO spans(trace_id, span_id, parent_id, service, name, start_us, duration_us, status, kind, attrs, events) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    counts["spans"] = len(rows)


ISSUES = [
    ("Payment gateway timeout during authorization", "payment-service /pay/authorize", "error", "critical", 1284, 312, 0.9, 0),
    ("pq: deadlock detected on payments insert", "checkout-service /checkout/submit", "error", "critical", 486, 132, 1, 0),
    ("TypeError: Cannot read properties of undefined (reading 'price')", "cart-service web/src/CartRow.tsx", "error", "high", 2310, 890, 0.9, 1),
    ("Redis connection reset by peer", "cart-service redis/client.py", "warning", "medium", 742, 208, 0.6, 2),
    ("Kafka NOT_LEADER_FOR_PARTITION on checkout.events", "notification-service kafka/producer.go", "error", "high", 193, 61, 1, 0),
    ("Search request exceeded 2s deadline", "search-service search/index.ts", "error", "high", 3118, 1044, 0.85, 2),
    ("JWT verification failed: token expired", "auth-service jwt/verify.go", "warning", "low", 5602, 2210, 0.4, 0),
    ("Inventory oversell guard triggered", "inventory-service reserve.go", "error", "critical", 88, 34, 1, 1),
    ("Email provider returned 429 Too Many Requests", "notification-service providers/email.go", "warning", "medium", 1420, 388, 0.5, 0),
    ("Unhandled promise rejection in checkout bootstrap", "web-checkout src/main.ts", "error", "high", 96, 44, 0.7, 2),
    ("Panic: nil pointer dereference in order worker", "checkout-service worker.go:214", "fatal", "critical", 41, 19, 1, 0),
    ("Postgres too many connections (max 200)", "postgres-primary connection slot", "error", "critical", 312, 77, 1, 1),
    ("Slow query: orders index scan over 1.2s", "postgres-primary orders", "warning", "medium", 2088, 640, 0.3, 1),
    ("Cache miss ratio above threshold", "cart-service cache/middleware.py", "warning", "low", 990, 202, 0.2, 2),
]


def _seed_issues(now, counts):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]:
        return
    out = []
    for i, (title, culprit, level, severity, count, users, ratio, stack_idx) in enumerate(ISSUES):
        active = i in (0, 1, 4, 11)
        first_seen = now - (1800 if active else random.randint(2, 26) * 86400)
        last_seen = now - (random.randint(20, 900) if active else random.randint(1, 40) * 3600)
        status = "unresolved" if active or i in (2, 5, 6, 9) else "resolved"
        tags = {"severity": severity, "service": culprit.split(" ")[0], "environment": "production", "runtime": "go" if stack_idx < 2 else "node"}
        cur = conn.execute(
            "INSERT INTO issues(fingerprint, title, culprit, level, service, first_seen, last_seen, count, users, status, assigned, release, stack, tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"fp_seed_{i}", title, culprit, level, culprit.split(" ")[0], first_seen, last_seen, count, users, status,
             random.choice(["", "Ade Okonkwo", "Priya Raman", "Chen Wu"]), f"v2.{random.randint(10, 19)}.{random.randint(0, 9)}",
             json.dumps(STACK_TEMPLATES[stack_idx]), json.dumps(tags)),
        )
        issue_id = cur.lastrowid
        events = []
        n = min(40, max(6, count // 20))
        for e in range(n):
            ts = last_seen - int((n - e) * (now - first_seen) / n * random.uniform(0.3, 1.0))
            if ts < first_seen:
                ts = first_seen + e
            events.append((issue_id, ts, "%032x" % random.getrandbits(128), f"u_{random.randint(1000,9999)}", f"v2.{random.randint(10,19)}.0",
                           json.dumps({"browser": random.choice(["Chrome 131", "Safari 18", "Firefox 133"]), "os": random.choice(["macOS 15", "Windows 11", "iOS 18"]), "region": random.choice(["us-east-1", "eu-west-1", "ap-south-1"])})))
        conn.executemany("INSERT INTO events(issue_id, ts, trace_id, user_id, release, context) VALUES(?,?,?,?,?,?)", events)
        out.append(issue_id)
    counts["issues"] = len(out)


def _seed_checks(self_url, now, counts):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]:
        return
    checks = [
        ("API gateway health", f"{self_url}/api/status/health", "GET", 200, 60, "Core API", 1),
        ("Checkout submit", f"{self_url}/api/status/echo?path=/v1/checkout", "GET", 200, 60, "Core API", 1),
        ("Search query", f"{self_url}/api/status/echo?path=/v1/search", "GET", 200, 120, "Core API", 1),
        ("Auth token endpoint", f"{self_url}/api/status/echo?path=/v1/auth", "GET", 200, 120, "Identity", 1),
        ("Checkout (EU region)", f"{self_url}/api/status/echo?path=/v1/checkout&region=eu", "GET", 200, 300, "Core API", 1),
        ("Marketing site", f"{self_url}/api/status/echo?path=/", "GET", 200, 300, "Web", 1),
    ]
    for name, url, method, expect, interval, group, public in checks:
        cur = conn.execute(
            "INSERT INTO checks(name, kind, url, method, expect_status, expect_body, interval_s, timeout_ms, regions, enabled, group_name, public) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "http", url, method, expect, "", interval, 5000, json.dumps(["iad", "fra"]), 1, group, public),
        )
        cid = cur.lastrowid
        results = []
        for minutes_ago in range(24 * 60, 0, -max(1, interval // 60)):
            ts = now - minutes_ago * 60
            down = False
            if "Checkout" in name and now - ts < 1800:
                down = random.random() < 0.22
            elif minutes_ago in (300, 301) and "Search" in name:
                down = True
            elif random.random() < 0.002:
                down = True
            latency = round(random.uniform(18, 95) * (3.2 if down else 1), 1)
            for region in ("iad", "fra"):
                results.append((cid, ts, 0 if down else 1, 504 if down else 200, latency, region, "timeout after 5000ms" if down else None))
        conn.executemany("INSERT INTO check_results(check_id, ts, ok, status_code, latency_ms, region, error) VALUES(?,?,?,?,?,?,?)", results)
        counts["checks"] += len(results)


def _seed_incidents(now):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]:
        return
    cur = conn.execute(
        "INSERT INTO incidents(reference, title, severity, status, impact, commander, responders, services, created_at, detected_at, summary) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("INC-0031", "Checkout error rate elevated — payment authorization failing", "SEV2", "investigating", "major",
         "Priya Raman", json.dumps(["Priya Raman", "Chen Wu", "Sofia Marino"]), json.dumps(["checkout-service", "payment-service", "api-gateway"]),
         now - 1500, now - 1620,
         "Checkout submissions are failing at ~6% with 503s from the payment gateway. Customers see a generic error after confirming payment."),
    )
    iid = cur.lastrowid
    events = [
        (iid, now - 1620, "declared", "Priya Raman", "SEV2 declared after HighErrorRate fired on checkout-service."),
        (iid, now - 1560, "commander", "Priya Raman", "I am incident commander. Chen is on payments, Sofia is on gateway."),
        (iid, now - 1500, "status", "Priya Raman", "Status set to investigating. Error ratio 6.2%, concentrated on POST /pay/authorize."),
        (iid, now - 1380, "comment", "Chen Wu", "Gateway latency is 2.6s at p99 and returning 504s. Deadlocks on payments insert correlate."),
        (iid, now - 1200, "escalation", "Sofia Marino", "Sofia Marino joined the response"),
        (iid, now - 900, "comment", "Chen Wu", "Raising connection pool and rolling back the payments retry storm. Watching error rate."),
        (iid, now - 420, "comment", "Priya Raman", "Error rate down to 6.0% from 6.8%. Not resolved yet."),
        (iid, now - 300, "link", "Priya Raman", "Dashboard: /d/service-overview?var-service=checkout-service"),
    ]
    conn.executemany("INSERT INTO incident_events(incident_id, ts, kind, actor, body) VALUES(?,?,?,?,?)", events)
    cur = conn.execute(
        "INSERT INTO incidents(reference, title, severity, status, impact, commander, responders, services, created_at, detected_at, resolved_at, summary, postmortem) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("INC-0030", "Search latency spike after index refresh", "SEV3", "resolved", "minor", "Marta Silva", json.dumps(["Marta Silva", "Yuki Sato"]),
         json.dumps(["search-service"]), now - 3 * 3600 - 1500, now - 3 * 3600 - 1400, now - 3 * 3600,
         "Search p95 rose to 1.9s for 25 minutes after a forced index refresh. Mitigated by throttling the refresh.", json.dumps({
             "author": "Marta Silva", "status": "in_review",
             "summary": "A manual index refresh at 09:12 saturated the search tier and pushed p95 to 1.9s.",
             "impact": "25 minutes of elevated search latency; 0.9% error rate on search. No data loss.",
             "root_cause": "Refresh ran with default throttle while the cluster was already at 78% CPU.",
             "action_items": [
                 {"owner": "Marta Silva", "item": "Throttle refresh to 20MB/s during business hours", "due": "2026-10-27", "done": True},
                 {"owner": "Yuki Sato", "item": "Add a pre-refresh CPU guard", "due": "2026-11-03", "done": False},
             ],
         })),
    )
    conn.execute("UPDATE incidents SET postmortem=postmortem WHERE id=?", (cur.lastrowid,))


def _seed_loadtest(now):
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM loadtests").fetchone()[0]:
        return
    started = now - 5 * 3600
    cur = conn.execute(
        "INSERT INTO loadtests(name, script, status, started_at, finished_at, summary, created_at) VALUES(?,?,?,?,?,?,?)",
        ("checkout baseline — 10 VU", json.dumps({"target": "/v1/checkout", "vus": 10, "duration_s": 30, "ramp": True, "method": "POST"}),
         "complete", started, started + 30,
         json.dumps({"requests": 1840, "errors": 22, "error_rate": 1.2, "rps": 61.3, "p50": 84.2, "p95": 190.4, "p99": 402.8, "duration_s": 30}), started),
    )
    tid = cur.lastrowid
    random.seed(7)
    pts = []
    for i in range(30):
        pts.append((tid, started + i, min(10, 1 + i), round(38 + i * 0.9 + random.uniform(-4, 4), 1), round(78 + random.uniform(-8, 8), 1), round(180 + random.uniform(-20, 40), 1), round(380 + random.uniform(-40, 120), 1), round(random.uniform(0.2, 2.4), 2)))
    conn.executemany("INSERT INTO loadtest_points(test_id, ts, vus, rps, p50, p95, p99, errors) VALUES(?,?,?,?,?,?,?,?)", pts)


def _seed_alerts(now):
    conn = db.connect()
    # evaluate in the past first so the seeded rules age past their `for` window and fire
    alerting.evaluate(now - 900)
    alerting.evaluate(now - 600)
    alerting.evaluate(now)
    # a resolved alert from earlier, for history
    row = conn.execute("SELECT id FROM rules WHERE name='LatencyP99High'").fetchone()
    if row and not conn.execute("SELECT COUNT(*) FROM alert_instances WHERE state='resolved'").fetchone()[0]:
        conn.execute(
            "INSERT INTO alert_instances(fingerprint, rule_id, name, severity, summary, labels, value, expr, state, active_since, last_eval, resolved_at, ends_at, acked_by, acked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("seed_resolved_1", row["id"], "LatencyP99High", "warning", "p99 latency above 800ms for 10 minutes",
             json.dumps({"service": "search-service", "verb": "GET"}), 1.92,
             'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service)) > 0.8',
             "resolved", now - 3 * 3600 - 1500, now - 3 * 3600 + 60, now - 3 * 3600, now - 3 * 3600, "Marta Silva", now - 3 * 3600 - 1200),
        )
    conn.execute(
        "INSERT INTO silences(fingerprint, matchers, starts_at, ends_at, created_by, comment) VALUES(?,?,?,?,?,?)",
        ("seed_silence_1", json.dumps([{"name": "service", "op": "=", "value": "notification-service"}, {"name": "severity", "op": "=", "value": "warning"}]),
         now - 3600, now + 6 * 3600, "Sofia Marino", "Provider maintenance window"),
    )


# ---------------------------------------------------------------------------
# Keeping the demo alive
# ---------------------------------------------------------------------------
def _clamp(metric: str, value: float) -> float:
    if metric == "up":
        return 1.0
    if metric.endswith("_ratio") or "usage_ratio" in metric:
        return min(max(value, 0.02), 0.99)
    if "free_bytes" in metric or "available_bytes" in metric.lower():
        return max(value, 1e8)
    if any(k in metric for k in ("in_flight", "queue_messages", "threads", "load1")):
        return max(value, 0.0)
    if "MemAvailable" in metric or "MemTotal" in metric:
        return max(value, 1e9)
    return value


def _continue_series(now: int) -> int:
    """Append one fresh point per series, extrapolating from the last two samples.

    Counters continue at their recent rate, gauges follow their trend with a little
    noise, and everything is clamped to a sane range. Nothing is invented out of
    thin air: the shape of the curve is the seeded shape.
    """
    conn = db.connect()
    latest = conn.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    if not latest:
        return 0
    rows = conn.execute(
        "SELECT x.series_id, x.ts, x.value, s.metric, s.labels, s.type "
        "FROM samples x JOIN series s ON s.id = x.series_id "
        "WHERE x.ts >= ? - 3600 ORDER BY x.series_id, x.ts",
        (latest,),
    ).fetchall()
    per: dict[int, list] = {}
    for r in rows:
        per.setdefault(r["series_id"], []).append(r)

    dt = max(now - latest, 1)
    written = 0
    with store.MetricWriter() as w:
        for series_id, points in per.items():
            last = points[-1]
            prev = points[-2] if len(points) > 1 else None
            metric = last["metric"]
            labels = json.loads(last["labels"] or "{}")
            value = float(last["value"])
            delta = value - float(prev["value"]) if prev else 0.0
            span = max(float(last["ts"] - prev["ts"]), 1.0) if prev else 0.0
            dt_now = max(float(now - last["ts"]), 1.0)
            per_second = delta / span if span else 0.0
            if last["type"] in ("counter", "histogram"):
                if delta <= 0:  # flat or reset: hold steady rather than invent growth
                    nxt = value
                else:
                    nxt = value + per_second * dt_now * random.uniform(0.85, 1.12)
            else:
                nxt = value + per_second * dt_now * 0.45 + (random.random() - 0.5) * max(abs(value) * 0.006, abs(delta) * 0.5)
            nxt = _clamp(metric, nxt)
            w.add(metric, labels, now, nxt, last["type"])
            written += 1
    return written


def _continue_logs(now: int, n: int = 6) -> int:
    conn = db.connect()
    recent = conn.execute(
        "SELECT * FROM logs WHERE ts >= ? - 3600 ORDER BY RANDOM() LIMIT ?", (now, n)
    ).fetchall()
    if not recent:
        recent = conn.execute("SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    rows = []
    for r in recent:
        rows.append((now + random.randint(0, 3), r["service"], r["level"], r["body"], r["labels"],
                     r["attrs"], r["trace_id"], r["span_id"]))
    conn.executemany("INSERT INTO logs(ts, service, level, body, labels, attrs, trace_id, span_id) VALUES(?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def _continue_spans(now: int, n: int = 2) -> int:
    conn = db.connect()
    traces = conn.execute(
        "SELECT trace_id FROM spans WHERE start_us >= ? GROUP BY trace_id ORDER BY RANDOM() LIMIT ?",
        ((now - 1800) * 1_000_000, n),
    ).fetchall()
    written = 0
    for t in traces:
        spans = conn.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY start_us", (t["trace_id"],)).fetchall()
        if not spans:
            continue
        new_tid = "%032x" % random.getrandbits(128)
        shift = now * 1_000_000 - min(s["start_us"] for s in spans)
        ids = {s["span_id"]: "%016x" % random.getrandbits(64) for s in spans}
        rows = []
        for s in spans:
            rows.append((
                new_tid, ids[s["span_id"]], ids.get(s["parent_id"]) if s["parent_id"] else None,
                s["service"], s["name"], s["start_us"] + shift, s["duration_us"], s["status"],
                s["kind"], s["attrs"], s["events"],
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO spans(trace_id, span_id, parent_id, service, name, start_us, duration_us, status, kind, attrs, events) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        written += len(rows)
    return written


def _rebase(delta: int) -> None:
    """Shift time-series, logs and spans forward so a sleeping laptop doesn't wake to empty charts."""
    conn = db.connect()
    conn.execute("BEGIN")
    # two-step shift: a plain `ts = ts + delta` can collide with a row that has not moved yet
    park = 10 ** 9
    conn.execute("UPDATE samples SET ts = ts + ?", (park,))
    conn.execute("UPDATE samples SET ts = ts - ?", (park - delta,))
    conn.execute("UPDATE series SET last_ts = last_ts + ?, first_ts = first_ts + ?", (delta, delta))
    conn.execute("UPDATE logs SET ts = ts + ?", (delta,))
    conn.execute("UPDATE spans SET start_us = start_us + ?", (delta * 1_000_000,))
    conn.execute("UPDATE check_results SET ts = ts + ?", (delta,))
    conn.execute("UPDATE loadtest_points SET ts = ts + ?", (delta,))
    conn.execute("COMMIT")
    try:  # a rebase rewrites the whole samples table; give the space back
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:  # noqa: BLE001
        pass


def live_tick(now: int | None = None) -> dict:
    """Keep the seeded environment moving. Called by the collector every minute."""
    if os.environ.get("CRAFT_LIVE", "1") == "0":
        return {"skipped": "disabled"}
    if not db.get_meta("seed_version"):
        return {"skipped": "no demo data"}
    now = int(now or db.now())
    conn = db.connect()
    latest = conn.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
    if not latest:
        return {"skipped": "no samples"}
    age = now - latest
    if age < 45:
        return {"skipped": "fresh"}
    rebased = 0
    if age > 180:  # a real gap (sleep, paused container): slide the window instead of drawing a cliff
        shift = age - 60
        _rebase(shift)
        rebased = shift
        latest += shift
        age = now - latest
    if age <= 0:
        return {"rebased_s": rebased, "samples": 0, "logs": 0, "spans": 0}
    samples = _continue_series(now)
    logs = _continue_logs(now)
    spans = _continue_spans(now)
    db.set_meta("live_last", now)
    return {"rebased_s": rebased, "samples": samples, "logs": logs, "spans": spans}
