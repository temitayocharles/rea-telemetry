"""Boots the real HTTP server on an ephemeral port and exercises the API."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("CRAFT_DB", os.path.join(tempfile.mkdtemp(), "api.db"))

from craft import db, seed  # noqa: E402
from craft import server as server_mod  # noqa: E402


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.reset()
        seed.seed("http://127.0.0.1:1", force=True)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, method="GET", body=None, raw=False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                return (payload if raw else json.loads(payload)), resp.status
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read()), exc.code

    def test_health(self):
        data, code = self.call("/api/health")
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "ok")

    def test_static_index(self):
        payload, code = self.call("/", raw=True)
        self.assertEqual(code, 200)
        self.assertIn(b"TelemetryCraft", payload)

    def test_prom_query(self):
        data, code = self.call("/api/prom/api/v1/query?query=sum(rate(http_requests_total[5m]))")
        self.assertEqual(code, 200)
        self.assertEqual(data["status"], "success")
        self.assertGreater(float(data["data"]["result"][0]["value"][1]), 0)

    def test_prom_range_and_labels(self):
        import time

        end = int(time.time())
        data, _ = self.call(f"/api/prom/api/v1/query_range?query=up&start={end - 3600}&end={end}&step=60")
        self.assertEqual(data["data"]["resultType"], "matrix")
        shapes, _ = self.call("/api/prom/api/v1/labels")
        self.assertIn("service", shapes["data"])
        values, _ = self.call("/api/prom/api/v1/label/service/values")
        self.assertIn("checkout-service", values["data"])

    def test_bad_query_is_400(self):
        data, code = self.call("/api/prom/api/v1/query?query=sum(")
        self.assertEqual(code, 400)
        self.assertIn("error", data)

    def test_overview(self):
        data, code = self.call("/api/overview")
        self.assertEqual(code, 200)
        self.assertGreater(data["headline"]["services"], 0)
        self.assertGreater(len(data["services"]), 0)
        self.assertTrue(data["traffic"]["rps"])

    def test_logs_and_traces(self):
        import time

        end = int(time.time())
        data, _ = self.call(f"/api/logs?query=%7Bservice%3D~%22.%2B%22%7D&start={end - 86400}&end={end}&limit=5")
        self.assertGreater(data["count"], 0)
        vol, _ = self.call(f"/api/logs/volume?query=%7Bservice%3D~%22.%2B%22%7D&start={end - 86400}&end={end}&step=300")
        self.assertTrue(vol["series"])
        traces, _ = self.call(f"/api/traces?start={end - 21600}&end={end}&limit=5")
        self.assertGreater(traces["count"], 0)
        detail, _ = self.call(f"/api/traces/{traces['traces'][0]['trace_id']}")
        self.assertTrue(detail["spans"])
        agg, _ = self.call(f"/api/traces/aggregates?start={end - 21600}&end={end}")
        self.assertTrue(agg["groups"])
        self.assertGreater(agg["groups"][0]["p95_ms"], 0)
        graph, _ = self.call(f"/api/traces/graph?start={end - 21600}&end={end}")
        self.assertTrue(graph["edges"])
        self.assertGreater(max(n["p95_ms"] for n in graph["nodes"]), 0)

    def test_dashboards_crud(self):
        listing, _ = self.call("/api/dashboards")
        self.assertGreaterEqual(len(listing["dashboards"]), 5)
        uid = listing["dashboards"][0]["uid"]
        detail, _ = self.call(f"/api/dashboards/{uid}")
        self.assertTrue(detail["panels"])
        saved, _ = self.call("/api/dashboards", "POST", {"uid": "test-uid", "title": "Test", "panels": [], "templating": []})
        self.assertTrue(saved["ok"])
        self.call("/api/dashboards/test-uid", "DELETE")
        missing, code = self.call("/api/dashboards/test-uid")
        self.assertEqual(code, 400)

    def test_alerts_and_rules(self):
        alerts, _ = self.call("/api/alerts")
        self.assertTrue(alerts["alerts"])
        created, _ = self.call("/api/rules", "POST", {"name": "APITest", "expr": "sum(up) > 0", "for": 0, "severity": "info"})
        self.assertIn("id", created)
        preview, _ = self.call("/api/rules/preview", "POST", {"expr": "sum(up)"})
        self.assertTrue(preview["ok"])
        _ = self.call(f"/api/rules/{created['id']}", "DELETE")
        evaluated, _ = self.call("/api/alerts/evaluate", "POST")
        self.assertEqual(evaluated["errors"], [])

    def test_incident_flow(self):
        created, _ = self.call("/api/incidents", "POST", {"title": "API test incident", "severity": "SEV3", "commander": "Tester", "services": ["api-gateway"]})
        self.assertTrue(created["reference"].startswith("INC-"))
        detail, _ = self.call(f"/api/incidents/{created['id']}")
        self.assertEqual(detail["status"], "investigating")
        updated, _ = self.call(f"/api/incidents/{created['id']}/status", "POST", {"status": "monitoring", "actor": "Tester"})
        self.assertEqual(updated["status"], "monitoring")
        pm, _ = self.call(f"/api/incidents/{created['id']}/postmortem", "POST", {"author": "Tester", "summary": "s", "action_items": [{"owner": "x", "item": "y", "done": False}]})
        self.assertEqual(pm["postmortem"]["summary"], "s")

    def test_slos(self):
        data, _ = self.call("/api/slos")
        self.assertTrue(data["slos"])
        row = data["slos"][0]
        self.assertIsNotNone(row["availability"])
        self.assertGreaterEqual(row["budget_remaining_pct"], 0)

    def test_checks_and_status_page(self):
        data, _ = self.call("/api/checks")
        self.assertTrue(data["checks"])
        ran, _ = self.call(f"/api/checks/{data['checks'][0]['id']}/run", "POST")
        self.assertIn("ok", ran)
        page, _ = self.call("/api/status-page")
        self.assertTrue(page["components"])

    def test_write_endpoints(self):
        import time

        ts = int(time.time())
        payload = {"timeseries": [{"metric": {"__name__": "api_test_metric", "job": "test"}, "values": [[ts * 1000, "11"]]}]}
        res, _ = self.call("/api/prom/api/v1/write", "POST", payload)
        self.assertEqual(res["accepted"], 1)
        written, _ = self.call(f"/api/prom/api/v1/query?query=api_test_metric")
        self.assertEqual(float(written["data"]["result"][0]["value"][1]), 11.0)
        simple, _ = self.call("/api/metrics/write", "POST", {"series": [{"metric": "simple_metric", "labels": {"a": "b"}, "value": 3}]})
        self.assertTrue(simple["ok"])
        logs, _ = self.call("/api/logs", "POST", [{"service": "api-test", "level": "info", "body": "hello"}])
        self.assertEqual(logs["accepted"], 1)
        event, _ = self.call("/api/events", "POST", {"title": "Boom", "service": "api-test", "level": "error"})
        self.assertIn("id", event)

    def test_search_and_metrics_catalog(self):
        catalog, _ = self.call("/api/explorer/metrics")
        self.assertTrue(catalog["metrics"])
        found, _ = self.call("/api/search?q=checkout")
        self.assertTrue(found["terms"] if "terms" in found else found["term"])

    def test_metrics_endpoint_is_prometheus_text(self):
        payload, code = self.call("/metrics", raw=True)
        self.assertEqual(code, 200)
        self.assertIn(b"telemetrycraft_series_total", payload)

    def test_saved_searches_and_paging(self):
        created, _ = self.call("/api/searches", "POST", {"app": "search", "name": "APITest search", "query": '{service="checkout-service"}'})
        self.assertTrue(created["ok"])
        listing, _ = self.call("/api/searches")
        self.assertTrue(any(s["id"] == created["id"] for s in listing["searches"]))
        _, code = self.call("/api/searches", "POST", {"name": "no query"})
        self.assertEqual(code, 400)
        removed, _ = self.call(f"/api/searches/{created['id']}", "DELETE")
        self.assertTrue(removed["ok"])
        paged, _ = self.call("/api/oncall/page", "POST", {"person": "API Tester", "title": "test page"})
        self.assertTrue(paged["ok"])
        self.assertEqual(paged["paged"], "API Tester")
        _, code = self.call("/api/oncall/page", "POST", {})
        self.assertEqual(code, 400)

    def test_export(self):
        data, _ = self.call("/api/export")
        self.assertIn("tables", data)
        self.assertIn("dashboards", data["tables"])


if __name__ == "__main__":
    unittest.main()
