"""Default dashboards, shipped as data (same panel model the UI renders)."""
from __future__ import annotations

import json

from . import db


def panel(pid, title, kind, targets, x, y, w, h, unit="short", legend="{{service}}", **extra):
    return {
        "id": pid,
        "title": title,
        "type": kind,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": chr(65 + i), "expr": t[0], "legend": t[1] if len(t) > 1 else legend} for i, t in enumerate(targets)],
        "unit": unit,
        "options": extra,
    }


SERVICE_VAR = [{"name": "service", "type": "query", "query": "label_values(service)", "multi": True, "includeAll": True}]


def dashboards() -> list[dict]:
    return [
        {
            "uid": "service-overview",
            "title": "Service Overview",
            "folder": "Platform",
            "tags": ["red", "golden-signals"],
            "templating": SERVICE_VAR,
            "panels": [
                panel(1, "Request rate", "timeseries",
                      [('sum(rate(http_requests_total{service=~"$service"}[5m])) by (service)', "{{service}}")], 0, 0, 8, 6, "reqps"),
                panel(2, "Error ratio", "timeseries",
                      [('sum(rate(http_requests_total{service=~"$service",status=~"5.."}[5m])) by (service) / sum(rate(http_requests_total{service=~"$service"}[5m])) by (service)', "{{service}}")], 8, 0, 8, 6, "percentunit"),
                panel(3, "Latency p50 / p95 / p99", "timeseries",
                      [
                          ('histogram_quantile(0.5, sum(rate(http_request_duration_seconds_bucket{service=~"$service"}[5m])) by (le))', "p50"),
                          ('histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{service=~"$service"}[5m])) by (le))', "p95"),
                          ('histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service=~"$service"}[5m])) by (le))', "p99"),
                      ], 16, 0, 8, 6, "s"),
                panel(4, "Availability (24h)", "stat",
                      [('avg(1 - (sum(rate(http_requests_total{service=~"$service",status=~"5.."}[5m])) by (service) / sum(rate(http_requests_total{service=~"$service"}[5m])) by (service)))', "availability")], 0, 6, 6, 4, "percentunit"),
                panel(5, "p99 latency (now)", "gauge",
                      [('histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service=~"$service"}[5m])) by (le))', "p99")], 6, 6, 6, 4, "s"),
                panel(6, "In-flight requests", "timeseries",
                      [('sum(http_requests_in_flight{service=~"$service"}) by (service)', "{{service}}")], 12, 6, 6, 4, "short"),
                panel(7, "CPU by service", "timeseries",
                      [('sum(app_cpu_usage_ratio{service=~"$service"}) by (service)', "{{service}}")], 18, 6, 6, 4, "percentunit"),
                panel(8, "Slowest operations", "table",
                      [('topk(10, histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service=~"$service"}[15m])) by (le, service, route)))', "{{service}} {{route}}")], 0, 10, 12, 6, "s"),
                panel(9, "Request volume by status", "timeseries",
                      [('sum(rate(http_requests_total{service=~"$service"}[5m])) by (status)', "{{status}}")], 12, 10, 12, 6, "reqps"),
            ],
        },
        {
            "uid": "http-golden-signals",
            "title": "HTTP Golden Signals",
            "folder": "Platform",
            "tags": ["http"],
            "templating": SERVICE_VAR,
            "panels": [
                panel(1, "Throughput vs errors", "timeseries",
                      [
                          ('sum(rate(http_requests_total[5m]))', "total"),
                          ('sum(rate(http_requests_total{status=~"5.."}[5m]))', "5xx"),
                          ('sum(rate(http_requests_total{status=~"4.."}[5m]))', "4xx"),
                      ], 0, 0, 12, 7, "reqps"),
                panel(2, "Latency heatmap", "heatmap",
                      [('sum(rate(http_request_duration_seconds_bucket[5m])) by (le)', "{{le}}")], 12, 0, 12, 7, "s"),
                panel(3, "Error budget burn", "timeseries",
                      [('sum(rate(http_requests_total{status=~"5.."}[1h])) / sum(rate(http_requests_total[1h]))', "burn")], 0, 7, 12, 6, "percentunit"),
                panel(4, "Requests by route", "bargauge",
                      [('topk(8, sum(rate(http_requests_total[10m])) by (route))', "{{route}}")], 12, 7, 12, 6, "reqps"),
            ],
        },
        {
            "uid": "host-resources",
            "title": "Host & Runtime",
            "folder": "Infrastructure",
            "tags": ["host", "node"],
            "templating": [{"name": "instance", "type": "query", "query": "label_values(instance)", "multi": True, "includeAll": True}],
            "panels": [
                panel(1, "CPU utilisation", "timeseries",
                      [('1 - avg(rate(node_cpu_seconds_total{mode="idle",instance=~"$instance"}[5m])) by (instance)', "{{instance}}")], 0, 0, 12, 6, "percentunit"),
                panel(2, "Memory available", "timeseries",
                      [('node_memory_MemAvailable_bytes{instance=~"$instance"}', "{{instance}}")], 12, 0, 12, 6, "bytes"),
                panel(3, "Filesystem free", "timeseries",
                      [('node_filesystem_free_bytes{instance=~"$instance"}', "{{instance}}")], 0, 6, 12, 6, "bytes"),
                panel(4, "Load average (1m)", "timeseries",
                      [('node_load1{instance=~"$instance"}', "{{instance}}")], 12, 6, 12, 6, "short"),
                panel(5, "Memory pressure", "gauge",
                      [('avg(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)', "used")], 0, 12, 8, 5, "percentunit"),
                panel(6, "Disk fill prediction", "stat",
                      [('predict_linear(node_filesystem_free_bytes[30m], 4*3600)', "free in 4h")], 8, 12, 8, 5, "bytes"),
                panel(7, "Scrape health", "stat",
                      [('up', "{{job}}")], 16, 12, 8, 5, "short"),
            ],
        },
        {
            "uid": "kubernetes-cluster",
            "title": "Kubernetes Cluster",
            "folder": "Infrastructure",
            "tags": ["kubernetes"],
            "templating": [{"name": "namespace", "type": "query", "query": "label_values(namespace)", "multi": True, "includeAll": True}],
            "panels": [
                panel(1, "Replicas available", "timeseries",
                      [('sum(kube_deployment_status_replicas_available{namespace=~"$namespace"}) by (deployment)', "{{deployment}}")], 0, 0, 12, 6, "short"),
                panel(2, "Container restarts", "timeseries",
                      [('sum(increase(kube_pod_container_status_restarts_total{namespace=~"$namespace"}[15m])) by (pod)', "{{pod}}")], 12, 0, 12, 6, "short"),
                panel(3, "Pods not ready", "stat",
                      [('sum(kube_deployment_status_replicas_unavailable{namespace=~"$namespace"})', "unavailable")], 0, 6, 6, 4, "short"),
                panel(4, "Nodes", "stat",
                      [('count(kube_node_info)', "nodes")], 6, 6, 6, 4, "short"),
                panel(5, "Workload restarts (top)", "table",
                      [('topk(10, kube_pod_container_status_restarts_total)', "{{pod}}")], 12, 6, 12, 4, "short"),
            ],
        },
        {
            "uid": "slo-error-budgets",
            "title": "SLO & Error Budgets",
            "folder": "Reliability",
            "tags": ["slo", "reliability"],
            "templating": SERVICE_VAR,
            "panels": [
                panel(1, "Error budget remaining", "bargauge",
                      [('1 - (sum(rate(http_requests_total{service=~"$service",status=~"5.."}[30d])) / sum(rate(http_requests_total{service=~"$service"}[30d]))) / 0.001', "{{service}}")], 0, 0, 12, 6, "percentunit"),
                panel(2, "Availability over 7d", "timeseries",
                      [('1 - (sum(rate(http_requests_total{service=~"$service",status=~"5.."}[30m])) by (service) / sum(rate(http_requests_total{service=~"$service"}[30m])) by (service))', "{{service}}")], 12, 0, 12, 6, "percentunit"),
                panel(3, "Burn rate (1h / 6h / 24h)", "timeseries",
                      [
                          ('(sum(rate(http_requests_total{status=~"5.."}[1h])) / sum(rate(http_requests_total[1h]))) / 0.001', "1h"),
                          ('(sum(rate(http_requests_total{status=~"5.."}[6h])) / sum(rate(http_requests_total[6h]))) / 0.001', "6h"),
                          ('(sum(rate(http_requests_total{status=~"5.."}[24h])) / sum(rate(http_requests_total[24h]))) / 0.001', "24h"),
                      ], 0, 6, 24, 6, "short"),
            ],
        },
        {
            "uid": "business-kpis",
            "title": "Business KPIs",
            "folder": "Product",
            "tags": ["business"],
            "templating": [],
            "panels": [
                panel(1, "Orders per minute", "timeseries",
                      [('sum(rate(checkout_orders_total[5m])) * 60', "orders/min")], 0, 0, 8, 6, "short"),
                panel(2, "Revenue per minute", "timeseries",
                      [('sum(rate(checkout_revenue_dollars_total[5m])) * 60', "revenue")], 8, 0, 8, 6, "currencyUSD"),
                panel(3, "Order success rate", "stat",
                      [('sum(rate(checkout_orders_total{status="success"}[1h])) / sum(rate(checkout_orders_total[1h]))', "success")], 16, 0, 8, 6, "percentunit"),
                panel(4, "Signups per hour", "timeseries",
                      [('sum(increase(signups_total[1h]))', "signups")], 0, 6, 12, 6, "short"),
                panel(5, "Queue backlog", "timeseries",
                      [('sum(queue_messages_ready) by (queue)', "{{queue}}")], 12, 6, 12, 6, "short"),
            ],
        },
        {
            "uid": "database-health",
            "title": "Database Health",
            "folder": "Infrastructure",
            "tags": ["postgres", "database"],
            "templating": [],
            "panels": [
                panel(1, "Connections vs max", "timeseries",
                      [
                          ('pg_stat_activity_count', "active"),
                          ('pg_settings_max_connections', "max"),
                      ], 0, 0, 12, 6, "short"),
                panel(2, "Connection saturation", "gauge",
                      [('pg_stat_activity_count / pg_settings_max_connections', "saturation")], 12, 0, 6, 6, "percentunit"),
                panel(3, "Cache hit ratio", "stat",
                      [('sum(rate(redis_keyspace_hits_total[5m])) / (sum(rate(redis_keyspace_hits_total[5m])) + sum(rate(redis_keyspace_misses_total[5m])))', "hit ratio")], 18, 0, 6, 6, "percentunit"),
                panel(4, "Transactions per second", "timeseries",
                      [('sum(rate(pg_stat_database_xact_commit_total[5m]))', "commits")], 0, 6, 12, 6, "short"),
                panel(5, "Redis memory used", "timeseries",
                      [('redis_memory_used_bytes', "used")], 12, 6, 12, 6, "bytes"),
            ],
        },
        {
            "uid": "logs-explorer",
            "title": "Logs & Errors",
            "folder": "Platform",
            "tags": ["logs"],
            "templating": SERVICE_VAR,
            "panels": [
                panel(1, "Log volume by level", "timeseries",
                      [('sum(count_over_time({service=~"$service"}[5m])) by (level)', "{{level}}")], 0, 0, 12, 6, "short"),
                panel(2, "Recent errors", "logs", [('{service=~"$service"} |~ "(?i)error|fail|timeout"', "errors")], 12, 0, 12, 6, "short"),
            ],
        },
    ]


def install_defaults(force: bool = False) -> int:
    conn = db.connect()
    if not force and conn.execute("SELECT COUNT(*) FROM dashboards").fetchone()[0]:
        return 0
    for d in dashboards():
        conn.execute(
            "INSERT OR REPLACE INTO dashboards(uid, title, folder, tags, panels, templating, updated_at, starred) VALUES(?,?,?,?,?,?,?,?)",
            (d["uid"], d["title"], d["folder"], json.dumps(d["tags"]), json.dumps(d["panels"]), json.dumps(d["templating"]), db.now(), 1 if d["uid"] == "service-overview" else 0),
        )
    return len(dashboards())
