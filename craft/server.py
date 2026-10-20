"""The single-binary server: REST API + ingest endpoints + background collector."""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import alerting, auth as authmod, db, dashboards, incidents, loadtest, promql, query, seed, slo, store, synthetics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH = authmod.Auth.from_env()
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
VERSION = "1.0.0"
STARTED = db.now()


def _int(params, key, default):
    raw = params.get(key, [default])[0]
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _float(params, key, default):
    raw = params.get(key, [default])[0]
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _time_ms(params, key, default):
    """Accept unix seconds, milliseconds, or RFC3339."""
    raw = params.get(key, [None])[0]
    if raw in (None, ""):
        return default
    try:
        val = float(raw)
        return int(val * 1000) if val < 1e12 else int(val)
    except ValueError:
        pass
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return default


class CraftServer(ThreadingHTTPServer):
    """Threaded server that shrugs off the browser hanging up mid-response."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(SimpleHTTPRequestHandler):
    server_version = f"TelemetryCraft/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter access logs
        if os.environ.get("CRAFT_VERBOSE"):
            super().log_message(fmt, *args)

    # ---- plumbing ------------------------------------------------------
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, data, code=200):
        self._send(code, json.dumps(data, separators=(",", ":")).encode())

    def error(self, message, code=400):
        self.json({"error": str(message), "status": code}, code)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.headers.get("Content-Encoding", "").lower() == "gzip" and raw:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        return raw

    def read_json(self):
        raw = self.read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    # ---- verbs ---------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, b"")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        if not path.startswith("/api/") and path not in ("/metrics",):
            if method in ("GET", "HEAD"):
                return self.serve_static(path)
            return self.error("not found", 404)
        client = self.client_address[0] if self.client_address else "?"
        if method != "OPTIONS":
            try:
                AUTH.check(self.headers, path, params)
            except authmod.AuthError as exc:
                AUTH.note_failure(client)
                if AUTH.throttle(client):
                    return self.error("too many failed authentication attempts", 429)
                return self._send(
                    401,
                    json.dumps({"error": str(exc), "status": 401}).encode(),
                    extra={"WWW-Authenticate": f'{exc.challenge} realm="TelemetryCraft"'},
                )
            if AUTH.enabled and AUTH.accepts(self.headers, params):
                # a good credential clears the lockout counter for this client
                AUTH.note_success(client)
        try:
            result = route(method, path, params, self)
        except ValueError as exc:
            return self.error(exc, 400)
        except Exception as exc:  # noqa: BLE001 - surface during development
            if os.environ.get("CRAFT_DEBUG"):
                import traceback

                traceback.print_exc()
            return self.error(f"{type(exc).__name__}: {exc}", 500)
        if result is None:
            return self.error("not found", 404)
        if isinstance(result, tuple):
            data, code = result[0], result[1]
            ctype = result[2] if len(result) > 2 else "application/json; charset=utf-8"
        else:
            data, code, ctype = result, 200, "application/json; charset=utf-8"
        if isinstance(data, bytes):
            return self._send(code, data, ctype)
        return self.json(data, code)

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not target.startswith(WEB) or not os.path.isfile(target):
            # SPA fallback
            target = os.path.join(WEB, "index.html")
            if not os.path.isfile(target):
                return self.error("web assets not found", 404)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
def route(method, path, params, h):
    # --- health / meta
    if path in ("/api/health", "/api/status/health"):
        return {"status": "ok", "service": "telemetrycraft", "version": VERSION, "uptime_s": db.now() - STARTED}
    if path == "/api/status/echo":
        path_echo = params.get("path", ["/"])[0]
        return {"ok": True, "path": path_echo, "region": params.get("region", ["iad"])[0], "ts": db.now()}
    if path == "/api/auth/status":
        return AUTH.status(getattr(h, "headers", None))
    if path == "/api/auth/login" and method == "POST":
        body = h.read_json()
        client = h.client_address[0] if h.client_address else "?"
        candidate = str(body.get("token") or "")
        if AUTH.token and AUTH.verify_token(candidate):
            # the client keeps the credential and replays it as a header
            AUTH.note_success(client)
            return {"ok": True, "method": "token", "credential": f"Bearer {candidate}"}
        if AUTH.user and AUTH.verify_basic(
            base64.b64encode(f"{body.get('user')}:{body.get('password')}".encode()).decode()
        ):
            encoded = base64.b64encode(f"{body.get('user')}:{body.get('password')}".encode()).decode()
            AUTH.note_success(client)
            return {"ok": True, "method": "basic", "credential": f"Basic {encoded}"}
        if not AUTH.enabled:
            return {"ok": True, "method": "none", "credential": ""}
        # login is public, so it counts failures itself instead of bypassing the lockout
        AUTH.note_failure(client)
        if AUTH.locked_out(client):
            return {"error": "too many failed authentication attempts", "status": 429}, 429
        return {"error": "invalid credentials", "status": 401}, 401

    if path == "/api/meta":
        return {
            "version": VERSION,
            "org": db.get_meta("org", {"name": "Acme Commerce"}),
            "uptime_s": db.now() - STARTED,
            "apps": APP_CATALOG,
            "seeded": db.get_meta("seed_version"),
        }
    if path == "/api/stats":
        return store.stats()
    if path == "/metrics":
        return self_metrics()

    # --- Prometheus compatible
    if path == "/api/prom/api/v1/query":
        expr = params.get("query", [""])[0]
        t = _time_ms(params, "time", db.now() * 1000)
        data = promql.query_instant(expr, t)
        return {"status": "success", "data": data}
    if path == "/api/prom/api/v1/query_range":
        expr = params.get("query", [""])[0]
        end = _time_ms(params, "end", db.now() * 1000)
        start = _time_ms(params, "start", end - 3600_000)
        step_raw = params.get("step", ["60"])[0]
        try:
            step_ms = promql.parse_duration(step_raw) if re.match(r"^\d+(ms|s|m|h|d|w|y)", step_raw) else int(float(step_raw) * 1000)
        except promql.ParseError:
            step_ms = 60000
        data = promql.query_range(expr, start, end, max(step_ms, 1000))
        return {"status": "success", "data": data}
    if path == "/api/prom/api/v1/labels":
        return {"status": "success", "data": promql.label_names()}
    m = re.match(r"^/api/prom/api/v1/label/([^/]+)/values$", path)
    if m:
        return {"status": "success", "data": promql.label_values(m.group(1))}
    if path == "/api/prom/api/v1/series":
        selector = params.get("match[]", ['{__name__=~".+"}'])[0]
        node = promql.parse(selector)
        if not isinstance(node, promql.Selector):
            raise ValueError("match[] must be a selector")
        series = promql.select_series(node.metric, node.matchers)
        return {"status": "success", "data": [s["labels"] for s in series[:5000]]}
    if path == "/api/prom/api/v1/metadata":
        return {"status": "success", "data": metric_metadata()}
    if path == "/api/prom/api/v1/write" and method == "POST":
        payload = h.read_json()
        n = store.ingest_remote_write(payload)
        return {"ok": True, "accepted": n}

    # --- OTLP / Loki / generic ingest
    if path in ("/api/otlp/v1/metrics", "/v1/metrics") and method == "POST":
        n = store.ingest_otlp_metrics(h.read_json())
        return {"ok": True, "accepted": n}
    if path in ("/api/otlp/v1/logs", "/v1/logs") and method == "POST":
        n = store.ingest_otlp_logs(h.read_json())
        return {"ok": True, "accepted": n}
    if path in ("/api/otlp/v1/traces", "/v1/traces") and method == "POST":
        n = store.ingest_otlp_traces(h.read_json())
        return {"ok": True, "accepted": n}
    if path == "/api/loki/api/v1/push" and method == "POST":
        return {"ok": True, "accepted": store.ingest_loki(h.read_json())}
    if path == "/api/metrics/write" and method == "POST":
        payload = h.read_json()
        points = payload.get("series") or payload.get("metrics") or []
        n = 0
        with store.MetricWriter() as w:
            for p in points:
                metric = p.get("metric") or p.get("name")
                if not metric:
                    continue
                labels = p.get("labels", {})
                vals = p.get("values") or [[p.get("ts", db.now() * 1000), p.get("value")]]
                for ts, val in vals:
                    w.add(metric, labels, float(ts), float(val))
                    n += 1
        return {"ok": True, "accepted": n}
    if path == "/api/logs" and method == "POST":
        payload = h.read_json()
        records = payload if isinstance(payload, list) else payload.get("logs", [])
        return {"ok": True, "accepted": store.ingest_json_logs(records)}
    if path == "/api/traces" and method == "POST":
        payload = h.read_json()
        records = payload if isinstance(payload, list) else payload.get("spans", [])
        return {"ok": True, "accepted": store.ingest_json_traces(records)}
    if path == "/api/events" and method == "POST":
        issue_id = store.ingest_event(h.read_json())
        return {"id": issue_id}

    # --- Metrics explorer
    if path == "/api/explorer/metrics":
        return {"metrics": metric_catalog()}
    if path == "/api/explorer/series":
        name = params.get("metric", [""])[0]
        series = promql.select_series(name, [])
        return {"metric": name, "series": [{"labels": {k: v for k, v in s["labels"].items() if k != "__name__"}} for s in series[:200]], "count": len(series)}
    if path == "/api/explorer/targets":
        if method == "POST":
            targets = h.read_json().get("targets", [])
            db.set_meta("scrape_targets", targets)
            return {"ok": True, "targets": targets}
        return {"targets": scrape_targets()}
    if path == "/api/explorer/scrape" and method == "POST":
        target = h.read_json()
        return store.scrape_once(target)

    # --- Logs
    if path == "/api/logs":
        q = params.get("query", ['{service=~".+"}'])[0]
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        limit = min(_int(params, "limit", 200), 2000)
        return query.query_logs(q, start, end, limit, params.get("direction", ["backward"])[0])
    if path == "/api/logs/volume":
        q = params.get("query", ['{service=~".+"}'])[0]
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        return query.log_volume(q, start, end, _int(params, "step", 60))
    if path == "/api/logs/stats":
        q = params.get("query", ['{service=~".+"}'])[0]
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        return query.log_stats(q, start, end)
    if path == "/api/logs/fields":
        rows = db.connect().execute("SELECT DISTINCT service FROM logs ORDER BY service").fetchall()
        levels = db.connect().execute("SELECT DISTINCT level FROM logs ORDER BY level").fetchall()
        return {"services": [r[0] for r in rows if r[0]], "levels": [r[0] for r in levels if r[0]]}

    # --- Traces
    if path == "/api/traces":
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        return query.search_traces(
            start, end,
            service=params.get("service", [""])[0],
            operation=params.get("operation", [""])[0],
            status=params.get("status", [""])[0],
            min_duration_ms=_float(params, "min_duration_ms", 0),
            max_duration_ms=_float(params, "max_duration_ms", 0),
            tag=params.get("tag", [""])[0],
            limit=min(_int(params, "limit", 50), 400),
            sort=params.get("sort", ["duration"])[0],
        )
    if path == "/api/traces/aggregates":
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        return query.trace_aggregates(start, end, params.get("group_by", ["service"])[0])
    if path == "/api/traces/volume":
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        return query.trace_volume(start, end, _int(params, "step", 60))
    if path == "/api/traces/graph":
        end = _int(params, "end", db.now())
        start = _int(params, "start", end - 3600)
        return query.service_graph(start, end)
    m = re.match(r"^/api/traces/([0-9a-fA-F]+)$", path)
    if m:
        trace = query.get_trace(m.group(1))
        if not trace:
            raise ValueError("trace not found")
        return trace

    # --- Errors / issues
    if path == "/api/issues":
        return list_issues(params)
    m = re.match(r"^/api/issues/(\d+)$", path)
    if m and method == "GET":
        return get_issue(int(m.group(1)))
    m = re.match(r"^/api/issues/(\d+)/status$", path)
    if m and method == "POST":
        status = h.read_json().get("status")
        if status not in ("unresolved", "resolved", "ignored"):
            raise ValueError("invalid status")
        db.connect().execute("UPDATE issues SET status=? WHERE id=?", (status, int(m.group(1))))
        return {"ok": True}
    m = re.match(r"^/api/issues/(\d+)/assign$", path)
    if m and method == "POST":
        db.connect().execute("UPDATE issues SET assigned=? WHERE id=?", (h.read_json().get("assigned", ""), int(m.group(1))))
        return {"ok": True}

    # --- Alerting
    if path == "/api/alerts":
        return {"alerts": alerting.list_alerts(params.get("state", [""])[0]), "last_eval": alerting.last_evaluation()}
    if path == "/api/alerts/evaluate" and method == "POST":
        return alerting.evaluate()
    m = re.match(r"^/api/alerts/(\d+)/ack$", path)
    if m and method == "POST":
        alerting.ack(int(m.group(1)), h.read_json().get("by", "operator"))
        return {"ok": True}
    m = re.match(r"^/api/alerts/(\d+)/incident$", path)
    if m and method == "POST":
        return incidents.declare_from_alert(int(m.group(1)), h.read_json().get("commander", ""))
    if path == "/api/rules":
        if method == "GET":
            return {"rules": alerting.list_rules()}
        rule = h.read_json()
        return {"id": alerting.upsert_rule(rule)}
    if path == "/api/rules/preview" and method == "POST":
        return alerting.rule_preview(h.read_json().get("expr", ""))
    m = re.match(r"^/api/rules/(\d+)$", path)
    if m and method == "DELETE":
        alerting.delete_rule(int(m.group(1)))
        return {"ok": True}
    if path == "/api/silences":
        if method == "GET":
            return {"silences": alerting.list_silences()}
        body = h.read_json()
        return alerting.create_silence(body.get("matchers", []), _int({"d": [str(body.get("duration_s", 7200))]}, "d", 7200), body.get("created_by", "operator"), body.get("comment", ""))
    m = re.match(r"^/api/silences/(\d+)$", path)
    if m and method == "DELETE":
        db.connect().execute("DELETE FROM silences WHERE id=?", (int(m.group(1)),))
        return {"ok": True}
    if path == "/api/notifications":
        return {"notifications": alerting.list_notifications(min(_int(params, "limit", 100), 500))}
    if path == "/api/receivers":
        if method == "GET":
            return {"receivers": alerting.receivers()}
        body = h.read_json()
        alerting.save_receivers(body.get("receivers", []))
        return {"ok": True, "receivers": alerting.receivers()}
    if path == "/api/receivers/test" and method == "POST":
        return alerting.test_route(h.read_json().get("receiver", ""))
    if path == "/api/oncall":
        return {"on_call": alerting.who_is_on_call()}
    if path == "/api/schedules":
        if method == "POST":
            body = h.read_json()
            conn = db.connect()
            conn.execute(
                "INSERT INTO schedules(name, rotation, members, handoff_hour) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET rotation=excluded.rotation, members=excluded.members, handoff_hour=excluded.handoff_hour",
                (body["name"], body.get("rotation", "weekly"), json.dumps(body.get("members", [])), int(body.get("handoff_hour", 9))),
            )
            return {"ok": True}
        rows = db.connect().execute("SELECT * FROM schedules ORDER BY name").fetchall()
        return {"schedules": [{**dict(r), "members": db.jloads(r["members"], [])} for r in rows]}

    # --- SLOs
    if path == "/api/slos":
        if method == "GET":
            return {"slos": slo.list_slos()}
        return {"id": slo.upsert_slo(h.read_json())}
    m = re.match(r"^/api/slos/(\d+)$", path)
    if m and method == "DELETE":
        slo.delete_slo(int(m.group(1)))
        return {"ok": True}

    # --- Synthetics & status page
    if path == "/api/checks":
        if method == "GET":
            return {"checks": synthetics.list_checks()}
        return {"id": synthetics.upsert_check(h.read_json())}
    m = re.match(r"^/api/checks/(\d+)$", path)
    if m and method == "DELETE":
        synthetics.delete_check(int(m.group(1)))
        return {"ok": True}
    m = re.match(r"^/api/checks/(\d+)/run$", path)
    if m and method == "POST":
        row = db.connect().execute("SELECT * FROM checks WHERE id=?", (int(m.group(1)),)).fetchone()
        if not row:
            raise ValueError("check not found")
        return synthetics.check_once(dict(row), "manual")
    if path == "/api/status-page":
        return synthetics.status_page()

    # --- Incidents
    if path == "/api/incidents":
        if method == "GET":
            return {"incidents": incidents.list_incidents(params.get("status", [""])[0])}
        body = h.read_json()
        return incidents.declare(body.get("title", "Untitled incident"), body.get("severity", "SEV3"), body.get("impact", "minor"), body.get("commander", ""), body.get("services", []), body.get("summary", ""))
    m = re.match(r"^/api/incidents/(\d+)$", path)
    if m and method == "GET":
        item = incidents.get_incident(int(m.group(1)))
        if not item:
            raise ValueError("incident not found")
        return item
    m = re.match(r"^/api/incidents/(\d+)/(status|events|commander|responders|postmortem)$", path)
    if m and method == "POST":
        iid, action = int(m.group(1)), m.group(2)
        body = h.read_json()
        if action == "status":
            incidents.update_status(iid, body.get("status", "investigating"), body.get("actor", ""), body.get("body", ""))
        elif action == "events":
            incidents.add_event(iid, body.get("kind", "comment"), body.get("actor", ""), body.get("body", ""))
        elif action == "commander":
            incidents.assign_commander(iid, body.get("commander", ""), body.get("actor", ""))
        elif action == "responders":
            incidents.add_responder(iid, body.get("person", ""))
        elif action == "postmortem":
            incidents.save_postmortem(iid, body)
        return incidents.get_incident(iid)
    if path == "/api/incidents/from-alert" and method == "POST":
        return incidents.declare_from_alert(int(h.read_json().get("alert_id", 0)))

    # --- Dashboards
    if path == "/api/dashboards":
        if method == "GET":
            return {"dashboards": list_dashboards()}
        body = h.read_json()
        return save_dashboard(body)
    m = re.match(r"^/api/dashboards/([\w-]+)$", path)
    if m:
        uid = m.group(1)
        if method == "GET":
            item = get_dashboard(uid)
            if not item:
                raise ValueError("dashboard not found")
            return item
        if method == "DELETE":
            db.connect().execute("DELETE FROM dashboards WHERE uid=?", (uid,))
            return {"ok": True}
    m = re.match(r"^/api/dashboards/([\w-]+)/star$", path)
    if m and method == "POST":
        conn = db.connect()
        row = conn.execute("SELECT starred FROM dashboards WHERE uid=?", (m.group(1),)).fetchone()
        if not row:
            raise ValueError("dashboard not found")
        conn.execute("UPDATE dashboards SET starred=? WHERE uid=?", (0 if row["starred"] else 1, m.group(1)))
        return {"ok": True, "starred": not row["starred"]}

    # --- Saved searches (Kibana/OpenSearch-style saved queries)
    if path == "/api/searches":
        if method == "GET":
            rows = db.connect().execute("SELECT * FROM saved_searches ORDER BY created_at DESC").fetchall()
            return {"searches": [dict(r) for r in rows]}
        body = h.read_json()
        if not body.get("name") or not body.get("query"):
            raise ValueError("name and query are required")
        conn = db.connect()
        conn.execute(
            "INSERT INTO saved_searches(app, name, query, created_at) VALUES(?,?,?,?)",
            (body.get("app", "search"), body["name"], body["query"], db.now()),
        )
        return {"ok": True, "id": conn.execute("SELECT last_insert_rowid()").fetchone()[0]}
    m = re.match(r"^/api/searches/(\d+)$", path)
    if m and method == "DELETE":
        db.connect().execute("DELETE FROM saved_searches WHERE id=?", (int(m.group(1)),))
        return {"ok": True}

    # --- Paging (manual page to whoever is on call)
    if path == "/api/oncall/page" and method == "POST":
        body = h.read_json()
        person = (body.get("person") or "").strip()
        if not person:
            raise ValueError("person is required")
        title = body.get("title") or "Manual page"
        message = body.get("message") or "Please acknowledge in the incident channel."
        db.connect().execute(
            "INSERT INTO notifications(ts, receiver, kind, title, body, status) VALUES(?,?,?,?,?,?)",
            (db.now(), "page:" + person, "page", title, message, "delivered"),
        )
        return {"ok": True, "paged": person, "title": title, "ts": db.now()}

    # --- Search
    if path == "/api/search":
        term = params.get("q", [""])[0].strip()
        if len(term) < 2:
            return {"term": term, "logs": [], "traces": [], "issues": [], "alerts": [], "dashboards": []}
        end = db.now()
        return query.search(term, end - 86400 * 7, end + 60, _int(params, "limit", 6))

    # --- Overview (home page)
    if path == "/api/overview":
        return overview()

    # --- Load tests
    if path == "/api/loadtests":
        if method == "GET":
            return {"tests": loadtest.list_tests()}
        body = h.read_json()
        return loadtest.start(body.get("name", "ad-hoc"), body.get("target", ""), body.get("vus", 5), body.get("duration_s", 20), body.get("ramp", True), body.get("method", "GET"))
    m = re.match(r"^/api/loadtests/(\d+)/cancel$", path)
    if m and method == "POST":
        loadtest.cancel(int(m.group(1)))
        return {"ok": True}

    # --- Collector / pipelines / settings
    if path == "/api/pipelines":
        return {"pipelines": list_pipelines()}
    if path == "/api/pipelines/save" and method == "POST":
        return save_pipeline(h.read_json())
    if path == "/api/settings":
        if method == "GET":
            return settings_payload()
        return save_settings(h.read_json())
    if path == "/api/collector/stats":
        return store.stats()
    if path == "/api/collector/prune" and method == "POST":
        return store.prune()

    # --- Demo data
    if path == "/api/demo/seed" and method == "POST":
        body = h.read_json() if int(h.headers.get("Content-Length") or 0) else {}
        host = h.headers.get("Host", "127.0.0.1")
        self_url = f"http://{host}" if not host.startswith("0.0.0.0") else "http://127.0.0.1:8787"
        return seed.seed(self_url, force=bool(body.get("force")))
    if path == "/api/demo/reset" and method == "POST":
        return {"reset": True}

    # --- Export
    if path == "/api/export":
        return export_snapshot()

    return None


APP_CATALOG = [
    {"id": "overview", "name": "Overview", "desc": "Your whole stack in one screen", "icon": "grid", "path": "/"},
    {"id": "explore", "name": "Metrics", "desc": "PromQL explorer and TSDB", "icon": "chart", "path": "/#/metrics"},
    {"id": "dashboards", "name": "Dashboards", "desc": "Panels, variables, layouts", "icon": "layout", "path": "/#/dashboards"},
    {"id": "logs", "name": "Logs", "desc": "LogQL search, live tail", "icon": "terminal", "path": "/#/logs"},
    {"id": "search", "name": "Search", "desc": "Discover-style log analytics", "icon": "search", "path": "/#/search"},
    {"id": "traces", "name": "Traces", "desc": "Distributed tracing, service map", "icon": "route", "path": "/#/traces"},
    {"id": "errors", "name": "Errors", "desc": "Issue tracking, stack traces", "icon": "alert", "path": "/#/errors"},
    {"id": "alerts", "name": "Alerting", "desc": "Rules, silences, routing", "icon": "bell", "path": "/#/alerts"},
    {"id": "oncall", "name": "On-call", "desc": "Rotations, escalations, paging", "icon": "phone", "path": "/#/oncall"},
    {"id": "incidents", "name": "Incidents", "desc": "Response, timeline, postmortems", "icon": "siren", "path": "/#/incidents"},
    {"id": "slo", "name": "SLOs", "desc": "Objectives and error budgets", "icon": "target", "path": "/#/slo"},
    {"id": "synthetics", "name": "Synthetics", "desc": "Uptime checks and status page", "icon": "globe", "path": "/#/synthetics"},
    {"id": "load", "name": "Load", "desc": "Run bounded load tests", "icon": "gauge", "path": "/#/load"},
    {"id": "pipeline", "name": "Collector", "desc": "Pipeline config and ingest", "icon": "pipe", "path": "/#/collector"},
    {"id": "settings", "name": "Settings", "desc": "Org, receivers, retention", "icon": "sliders", "path": "/#/settings"},
]


def metric_catalog() -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT metric, COUNT(*) AS series, type, MAX(last_ts) AS last_ts FROM series GROUP BY metric ORDER BY metric"
    ).fetchall()
    samples = dict(conn.execute("SELECT s.metric, COUNT(*) FROM samples sm JOIN series s ON s.id=sm.series_id GROUP BY s.metric").fetchall())
    return [
        {"name": r["metric"], "series": r["series"], "type": r["type"], "samples": samples.get(r["metric"], 0), "last_ts": r["last_ts"]}
        for r in rows
    ]


def metric_metadata() -> dict:
    meta = {}
    for item in metric_catalog():
        meta[item["name"]] = [{"type": item["type"], "help": f"{item['name']} (ingested)", "unit": ""}]
    return meta


def scrape_targets() -> list[dict]:
    return db.get_meta("scrape_targets", [
        {"url": "/metrics", "job": "telemetrycraft", "instance": "self", "interval_s": 30, "enabled": True},
    ])


def list_issues(params) -> dict:
    conn = db.connect()
    where, args = [], []
    status = params.get("status", [""])[0]
    if status:
        where.append("status=?")
        args.append(status)
    if params.get("query", [""])[0]:
        term = f"%{params['query'][0]}%"
        where.append("(title LIKE ? OR culprit LIKE ?)")
        args += [term, term]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""SELECT i.*, (SELECT COUNT(*) FROM events e WHERE e.issue_id=i.id AND e.ts > ?) AS events_24h
            FROM issues i {clause} ORDER BY
            CASE i.status WHEN 'unresolved' THEN 0 ELSE 1 END, i.last_seen DESC""",
        (db.now() - 86400, *args),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "title": r["title"],
                "culprit": r["culprit"],
                "level": r["level"],
                "service": r["service"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
                "count": r["count"],
                "users": r["users"],
                "status": r["status"],
                "assigned": r["assigned"],
                "release": r["release"],
                "tags": db.jloads(r["tags"], {}),
                "events_24h": r["events_24h"],
                "sparkline": _issue_spark(conn, r["id"]),
            }
        )
    return {"issues": out}


def _issue_spark(conn, issue_id: int) -> list[int]:
    end = db.now()
    buckets = [0] * 24
    for row in conn.execute("SELECT ts FROM events WHERE issue_id=? AND ts>?", (issue_id, end - 86400)):
        idx = min(23, int((end - row["ts"]) // 3600))
        buckets[23 - idx] += 1
    return buckets


def get_issue(issue_id: int) -> dict:
    conn = db.connect()
    r = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not r:
        raise ValueError("issue not found")
    events = conn.execute("SELECT * FROM events WHERE issue_id=? ORDER BY ts DESC LIMIT 25", (issue_id,)).fetchall()
    users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE issue_id=? AND user_id<>''", (issue_id,)).fetchone()[0]
    return {
        "id": r["id"],
        "title": r["title"],
        "culprit": r["culprit"],
        "level": r["level"],
        "service": r["service"],
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "count": r["count"],
        "users": users or r["users"],
        "status": r["status"],
        "assigned": r["assigned"],
        "release": r["release"],
        "stack": db.jloads(r["stack"], []),
        "tags": db.jloads(r["tags"], {}),
        "events": [
            {"ts": e["ts"], "trace_id": e["trace_id"], "user_id": e["user_id"], "release": e["release"], "context": db.jloads(e["context"], {})}
            for e in events
        ],
        "sparkline": _issue_spark(conn, issue_id),
    }


def list_dashboards() -> list[dict]:
    conn = db.connect()
    rows = conn.execute("SELECT uid, title, folder, tags, panels, templating, updated_at, starred FROM dashboards ORDER BY starred DESC, title").fetchall()
    out = []
    for r in rows:
        panels = db.jloads(r["panels"], [])
        out.append(
            {
                "uid": r["uid"],
                "title": r["title"],
                "folder": r["folder"],
                "tags": db.jloads(r["tags"], []),
                "panels": panels,
                "panel_count": len(panels),
                "templating": db.jloads(r["templating"], []),
                "updated_at": r["updated_at"],
                "starred": bool(r["starred"]),
            }
        )
    return out


def get_dashboard(uid: str) -> dict | None:
    conn = db.connect()
    r = conn.execute("SELECT * FROM dashboards WHERE uid=?", (uid,)).fetchone()
    if not r:
        return None
    return {
        "uid": r["uid"],
        "title": r["title"],
        "folder": r["folder"],
        "tags": db.jloads(r["tags"], []),
        "panels": db.jloads(r["panels"], []),
        "templating": db.jloads(r["templating"], []),
        "starred": bool(r["starred"]),
        "updated_at": r["updated_at"],
    }


def save_dashboard(body: dict) -> dict:
    conn = db.connect()
    uid = body.get("uid") or re.sub(r"[^a-z0-9-]+", "-", (body.get("title") or "dashboard").lower()).strip("-")
    existing = conn.execute("SELECT uid FROM dashboards WHERE uid=?", (uid,)).fetchone()
    values = (
        body.get("title", "Untitled"),
        body.get("folder", "General"),
        json.dumps(body.get("tags", [])),
        json.dumps(body.get("panels", [])),
        json.dumps(body.get("templating", [])),
        db.now(),
    )
    if existing:
        conn.execute("UPDATE dashboards SET title=?, folder=?, tags=?, panels=?, templating=?, updated_at=? WHERE uid=?", (*values, uid))
    else:
        conn.execute("INSERT INTO dashboards(uid, title, folder, tags, panels, templating, updated_at) VALUES(?,?,?,?,?,?,?)", (uid, *values))
    return {"ok": True, "uid": uid}


def list_pipelines() -> list[dict]:
    rows = db.connect().execute("SELECT * FROM pipelines ORDER BY signal, name").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "signal": r["signal"],
            "receivers": db.jloads(r["receivers"], []),
            "processors": db.jloads(r["processors"], []),
            "exporters": db.jloads(r["exporters"], []),
            "enabled": bool(r["enabled"]),
        }
        for r in rows
    ]


def save_pipeline(body: dict) -> dict:
    conn = db.connect()
    conn.execute(
        "INSERT INTO pipelines(name, signal, receivers, processors, exporters, enabled) VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET signal=excluded.signal, receivers=excluded.receivers, processors=excluded.processors, exporters=excluded.exporters, enabled=excluded.enabled",
        (body["name"], body.get("signal", "metrics"), json.dumps(body.get("receivers", [])), json.dumps(body.get("processors", [])), json.dumps(body.get("exporters", [])), 1 if body.get("enabled", True) else 0),
    )
    return {"ok": True}


def settings_payload() -> dict:
    return {
        "org": db.get_meta("org", {"name": "Acme Commerce"}),
        "collector": db.get_meta("collector", {}),
        "receivers": alerting.receivers(),
        "on_call": alerting.who_is_on_call(),
        "retention_days": int(os.environ.get("CRAFT_RETENTION_DAYS", 30)),
        "storage": store.stats(),
        "ingest_endpoints": [
            {"name": "Prometheus remote-write", "url": "/api/prom/api/v1/write", "format": "JSON"},
            {"name": "Prometheus text scrape", "url": "/metrics", "format": "text/plain"},
            {"name": "OTLP metrics/logs/traces", "url": "/api/otlp/v1/{signal}", "format": "Protobuf-JSON"},
            {"name": "Loki push", "url": "/api/loki/api/v1/push", "format": "JSON"},
            {"name": "Simple metrics", "url": "/api/metrics/write", "format": "JSON"},
            {"name": "Error events", "url": "/api/events", "format": "JSON"},
        ],
    }


def save_settings(body: dict) -> dict:
    if "org" in body:
        db.set_meta("org", body["org"])
    if "collector" in body:
        db.set_meta("collector", body["collector"])
    if "receivers" in body:
        alerting.save_receivers(body["receivers"])
    if "retention_days" in body:
        db.set_meta("retention_days", int(body["retention_days"]))
        os.environ["CRAFT_RETENTION_DAYS"] = str(body["retention_days"])
    return settings_payload()


def overview() -> dict:
    now = db.now()
    conn = db.connect()

    def q(expr, ms=now * 1000):
        try:
            return promql.query_instant(expr, ms).get("result", [])
        except Exception:  # noqa: BLE001
            return []

    total = q('sum(rate(http_requests_total[5m]))')
    errors = q('sum(rate(http_requests_total{status=~"5.."}[5m]))')
    rps = float(total[0]["value"][1]) if total else 0.0
    err_rate = (float(errors[0]["value"][1]) / rps) if errors and rps else 0.0
    p99 = q('histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))')
    p50 = q('histogram_quantile(0.50, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))')
    per_service = q('sum(rate(http_requests_total[5m])) by (service)')
    svc_errors = {(s["metric"].get("service")): float(s["value"][1]) for s in q('sum(rate(http_requests_total{status=~"5.."}[5m])) by (service)')}
    services = []
    meta = {r["name"]: r for r in conn.execute("SELECT * FROM services")}
    for s in per_service:
        name = s["metric"].get("service", "")
        rate = float(s["value"][1])
        err = svc_errors.get(name, 0.0)
        row = meta.get(name)
        services.append(
            {
                "name": name,
                "display": row["display"] if row else name,
                "team": row["team"] if row else "",
                "tier": row["tier"] if row else 3,
                "rps": round(rate, 2),
                "error_rate": round(err / rate * 100, 3) if rate else 0,
            }
        )
    services.sort(key=lambda x: -x["rps"])
    alerts = alerting.list_alerts()
    slos = slo.list_slos()
    open_inc = incidents.open_incidents()
    status = synthetics.status_page()
    history = promql.query_range('sum(rate(http_requests_total[5m]))', (now - 6 * 3600) * 1000, now * 1000, 300_000)
    err_history = promql.query_range('sum(rate(http_requests_total{status=~"5.."}[5m]))', (now - 6 * 3600) * 1000, now * 1000, 300_000)
    lat_history = promql.query_range('histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))', (now - 6 * 3600) * 1000, now * 1000, 300_000)
    return {
        "generated_at": now,
        "headline": {
            "rps": round(rps, 2),
            "error_rate": round(err_rate * 100, 3),
            "p50_ms": round(float(p50[0]["value"][1]) * 1000, 1) if p50 else 0,
            "p99_ms": round(float(p99[0]["value"][1]) * 1000, 1) if p99 else 0,
            "services": len(services),
            "firing": sum(1 for a in alerts if a["state"] == "firing"),
            "pending": sum(1 for a in alerts if a["state"] == "pending"),
            "incidents": len(open_inc),
            "unresolved_issues": conn.execute("SELECT COUNT(*) FROM issues WHERE status='unresolved'").fetchone()[0],
            "uptime_24h": round(sum(c["uptime_pct"] for c in status["components"]) / max(len(status["components"]), 1), 3),
            "status": status["status"],
        },
        "services": services,
        "alerts": alerts[:8],
        "slos": [
            {"name": s["name"], "service": s["service"], "objective": s["objective"], "availability": s.get("availability"), "budget_remaining_pct": s.get("budget_remaining_pct"), "status": s.get("status"), "window_days": s["window_days"]}
            for s in slos
        ],
        "incidents": open_inc[:4],
        "on_call": alerting.who_is_on_call(),
        "traffic": {"rps": history["result"], "errors": err_history["result"], "p95": lat_history["result"]},
        "status_page": {"status": status["status"], "components": status["components"]},
    }


def self_metrics() -> tuple[bytes, int, str]:
    st = store.stats()
    lines = [
        "# HELP telemetrycraft_series_total Active time series.",
        "# TYPE telemetrycraft_series_total gauge",
        f"telemetrycraft_series_total {st['series']}",
        "# HELP telemetrycraft_samples_total Stored samples.",
        "# TYPE telemetrycraft_samples_total gauge",
        f"telemetrycraft_samples_total {st['samples']}",
        "# HELP telemetrycraft_logs_total Stored log lines.",
        "# TYPE telemetrycraft_logs_total gauge",
        f"telemetrycraft_logs_total {st['logs']}",
        "# HELP telemetrycraft_spans_total Stored spans.",
        "# TYPE telemetrycraft_spans_total gauge",
        f"telemetrycraft_spans_total {st['spans']}",
        "# HELP telemetrycraft_alerts_firing Alerts currently firing.",
        "# TYPE telemetrycraft_alerts_firing gauge",
        f"telemetrycraft_alerts_firing {st['alerts_firing']}",
        "# HELP telemetrycraft_uptime_seconds Process uptime.",
        "# TYPE telemetrycraft_uptime_seconds counter",
        f"telemetrycraft_uptime_seconds {db.now() - STARTED}",
        "# HELP telemetrycraft_db_bytes Bytes on disk.",
        "# TYPE telemetrycraft_db_bytes gauge",
        f"telemetrycraft_db_bytes {st['db_bytes']}",
    ]
    return ("\n".join(lines) + "\n").encode(), 200, "text/plain; charset=utf-8"


def export_snapshot() -> dict:
    conn = db.connect()
    tables = ["series", "logs", "issues", "rules", "alert_instances", "silences", "schedules", "services", "slos", "checks", "check_results", "incidents", "incident_events", "dashboards", "pipelines"]
    out = {"exported_at": db.now(), "version": VERSION, "tables": {}}
    for t in tables:
        rows = conn.execute(f"SELECT * FROM {t} LIMIT 5000").fetchall()
        out["tables"][t] = [dict(r) for r in rows]
    return out


# --------------------------------------------------------------------------
# Collector loop
# --------------------------------------------------------------------------
class Collector:
    def __init__(self, interval: float = 15.0):
        self.interval = interval
        self.stop = threading.Event()
        self.last = {"alert_eval": 0, "synthetics": 0, "prune": 0, "live": 0}

    def run_once(self):
        now = db.now()
        try:
            alerting.evaluate(now)
            self.last["alert_eval"] = now
        except Exception:  # noqa: BLE001
            pass
        try:
            synthetics.run_due()
            self.last["synthetics"] = now
        except Exception:  # noqa: BLE001
            pass
        if now - self.last["live"] > 60:
            try:
                seed.live_tick(now)
                self.last["live"] = now
            except Exception:  # noqa: BLE001
                pass
        if now - self.last["prune"] > 3600:
            try:
                store.prune()
                self.last["prune"] = now
            except Exception:  # noqa: BLE001
                pass

    def loop(self):
        while not self.stop.wait(self.interval):
            self.run_once()


def serve(port: int = 8787, host: str = "0.0.0.0", autoseed: bool = True, tls: dict | None = None):
    try:
        # default rules, plus the idempotent backfills they carry
        alerting.seed_defaults()
    except Exception as exc:  # noqa: BLE001
        print(f"[rules] skipped defaults: {exc}")
    if autoseed:
        try:
            seed.seed(f"http://127.0.0.1:{port}")
        except Exception as exc:  # noqa: BLE001
            print(f"[seed] skipped: {exc}")
    collector = Collector()
    threading.Thread(target=collector.loop, name="craft-collector", daemon=True).start()
    synthetics.start_worker()
    tls = tls or {}
    context = authmod.tls_context(
        tls.get("cert", ""), tls.get("key", ""), tls.get("client_ca", "")
    )
    httpd = CraftServer((host, port), Handler)
    if context is not None:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    scheme = authmod.scheme_for(context)
    lock = f"  auth: {AUTH.method}" if AUTH.enabled else "  auth: off"
    print(f"TelemetryCraft {VERSION} listening on {scheme}://{host}:{port}  (db: {db.db_path()}){lock}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop.set()
        synthetics.stop_worker()


def _apply_auth_flags(args) -> None:
    """CLI flags win over environment variables."""
    global AUTH
    if getattr(args, "auth_token", None) or getattr(args, "auth_user", None):
        AUTH = authmod.Auth(
            token=args.auth_token or "",
            user=args.auth_user or "",
            password=getattr(args, "auth_password", "") or "",
        )
    elif getattr(args, "no_auth", False):
        AUTH = authmod.Auth()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="craft", description="TelemetryCraft server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--no-seed", action="store_true")
    ap.add_argument("--auth-token", help="require this bearer token (or set CRAFT_AUTH_TOKEN)")
    ap.add_argument("--auth-user", help="require HTTP basic auth with this user")
    ap.add_argument("--auth-password", help="password for --auth-user")
    ap.add_argument("--no-auth", action="store_true", help="disable auth even if the environment enables it")
    ap.add_argument("--tls-cert", help="PEM certificate (or set CRAFT_TLS_CERT)")
    ap.add_argument("--tls-key", help="PEM private key (or set CRAFT_TLS_KEY)")
    ap.add_argument("--tls-client-ca", help="require client certificates signed by this CA")
    args = ap.parse_args(argv)
    if args.auth_user and not (args.auth_password or os.environ.get("CRAFT_AUTH_PASSWORD")):
        ap.error("--auth-user needs --auth-password (or CRAFT_AUTH_PASSWORD)")
    _apply_auth_flags(args)
    serve(args.port, args.host, autoseed=not args.no_seed,
          tls={"cert": args.tls_cert or "", "key": args.tls_key or "", "client_ca": args.tls_client_ca or ""})


if __name__ == "__main__":
    main()
