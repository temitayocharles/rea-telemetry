import os
import tempfile
import unittest

os.environ.setdefault("CRAFT_DB", os.path.join(tempfile.mkdtemp(), "ingest.db"))

from craft import alerting, db, incidents, promql, query, store, synthetics  # noqa: E402


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.reset()

    def test_remote_write(self):
        now = db.now()
        payload = {"timeseries": [{"metric": {"__name__": "rw_total", "job": "t"}, "values": [[now * 1000, "5"]]}]}
        self.assertEqual(store.ingest_remote_write(payload), 1)
        res = promql.query_instant("rw_total", now * 1000)
        self.assertEqual(float(res["result"][0]["value"][1]), 5.0)

    def test_prom_text(self):
        text = '# HELP x\ndemo_metric{job="a",le="0.5"} 3.5 1700000000000\n'
        rows = store.parse_prom_text(text)
        self.assertEqual(rows[0][0], "demo_metric")
        self.assertEqual(rows[0][1]["le"], "0.5")

    def test_otlp_metrics_and_logs(self):
        now_ns = str(db.now() * 1_000_000_000)
        payload = {
            "resourceMetrics": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "otlp-svc"}}]},
                "scopeMetrics": [{"metrics": [{"name": "otlp_gauge", "gauge": {"dataPoints": [{"asDouble": 7, "timeUnixNano": now_ns}]}}]}],
            }]
        }
        self.assertEqual(store.ingest_otlp_metrics(payload), 1)
        res = promql.query_instant("otlp_gauge", db.now() * 1000)
        self.assertEqual(float(res["result"][0]["value"][1]), 7.0)

        logs = {
            "resourceLogs": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "otlp-svc"}}]},
                "scopeLogs": [{"logRecords": [{"timeUnixNano": now_ns, "severityText": "ERROR", "body": {"stringValue": "boom"}}]}],
            }]
        }
        self.assertEqual(store.ingest_otlp_logs(logs), 1)
        found = query.query_logs('{service="otlp-svc"}', db.now() - 60, db.now() + 10)
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["entries"][0]["level"], "error")

    def test_loki(self):
        now_ns = str(db.now() * 1_000_000_000)
        payload = {"streams": [{"stream": {"service_name": "loki-svc"}, "values": [[now_ns, '{"level":"warn","msg":"slow"']]}]}
        # malformed JSON body stays as the raw line
        store.ingest_loki(payload)
        found = query.query_logs('{service="loki-svc"}', db.now() - 60, db.now() + 10)
        self.assertEqual(found["count"], 1)

    def test_otlp_traces(self):
        now_ns = str(db.now() * 1_000_000_000)
        payload = {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "trace-svc"}}]},
                "scopeSpans": [{"spans": [
                    {"traceId": "t1", "spanId": "s1", "name": "GET /x", "startTimeUnixNano": now_ns, "endTimeUnixNano": str(int(now_ns) + 5_000_000)},
                    {"traceId": "t1", "spanId": "s2", "parentSpanId": "s1", "name": "db", "startTimeUnixNano": now_ns, "endTimeUnixNano": str(int(now_ns) + 2_000_000)},
                ]}],
            }]
        }
        self.assertEqual(store.ingest_otlp_traces(payload), 2)
        trace = query.get_trace("t1")
        self.assertEqual(len(trace["spans"]), 2)
        self.assertEqual(trace["spans"][0]["duration_ms"], 5.0)

    def test_error_grouping(self):
        payload = {"title": "TypeError", "message": "x is undefined", "service": "web", "level": "error", "user_id": "u1", "stack": [{"module": "a.js", "function": "f", "line": 3}]}
        first = store.ingest_event(dict(payload))
        second = store.ingest_event(dict(payload, user_id="u2"))
        self.assertEqual(first, second)
        row = db.connect().execute("SELECT count, users FROM issues WHERE id=?", (first,)).fetchone()
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["users"], 2)

    def test_retention(self):
        old = db.now() - 10 * 86400
        with store.MetricWriter() as w:
            w.add("old_metric", {}, old, 1.0)
        cuts = store.prune(5)
        self.assertGreaterEqual(cuts["samples"], 1)


class AlertingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.reset()
        alerting.upsert_rule({"name": "QueueBacklog", "expr": "sum(queue_messages_ready) by (queue) > 5000", "for": 60, "severity": "warning", "summary": "backlog", "group": "mq"})
        alerting.upsert_rule({"name": "SilencedRule", "expr": "sum(queue_messages_ready) by (queue) > 5000", "for": 0, "severity": "warning", "summary": "noise", "group": "mq"})

    @staticmethod
    def seed_backlog(queue: str):
        now = db.now()
        with store.MetricWriter() as w:
            for i in range(40):
                w.add("queue_messages_ready", {"queue": queue}, now - 300 + i * 8, 9000)

    def alert_for(self, queue):
        return next((a for a in alerting.list_alerts() if a["labels"].get("queue") == queue and a["name"] == "QueueBacklog"), None)

    def fire(self, queue):
        self.seed_backlog(queue)
        now = db.now()
        alerting.evaluate(now)
        alerting.evaluate(now + 61)
        return self.alert_for(queue)

    def test_pending_then_firing(self):
        self.seed_backlog("pending-q")
        now = db.now()
        res = alerting.evaluate(now)
        self.assertEqual(res["errors"], [])
        self.assertEqual(self.alert_for("pending-q")["state"], "pending")
        alerting.evaluate(now + 61)
        self.assertEqual(self.alert_for("pending-q")["state"], "firing")
        self.assertTrue(any(n["title"].startswith("[WARNING]") for n in alerting.list_notifications()))

    def test_acknowledge_and_incident(self):
        alert = self.fire("ack-q")
        self.assertEqual(alert["state"], "firing")
        alerting.ack(alert["id"], "tester")
        self.assertEqual(self.alert_for("ack-q")["acked_by"], "tester")
        res = incidents.declare_from_alert(alert["id"], "Commander")
        incident = incidents.get_incident(res["id"])
        self.assertEqual(incident["severity"], "SEV3")
        self.assertEqual(incident["timeline"][0]["kind"], "declared")
        incidents.update_status(res["id"], "resolved", "tester", "done")
        self.assertIsNotNone(incidents.get_incident(res["id"])["resolved_at"])

    def test_silence_suppresses_delivery(self):
        db.connect().execute("DELETE FROM notifications")
        alerting.create_silence([{"name": "alertname", "op": "=", "value": "SilencedRule"}], 3600, "tester", "noise")
        self.assertTrue(alerting.list_silences(active_only=True))
        self.seed_backlog("silenced-q")
        alerting.evaluate(db.now() + 200)
        silenced = [n for n in alerting.list_notifications(500) if n["alert_name"] == "SilencedRule"]
        self.assertTrue(silenced)
        self.assertTrue(all(n["status"] == "silenced" for n in silenced))

    def test_rule_validation(self):
        with self.assertRaises(promql.ParseError):
            alerting.upsert_rule({"name": "Bad", "expr": "sum(", "for": 0})


class SyntheticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.reset()

    def test_check_roundtrip(self):
        check_id = synthetics.upsert_check({"name": "self", "url": "http://127.0.0.1:1/health", "expect_status": 200, "interval_s": 60, "regions": ["iad"], "timeout_ms": 300})
        result = synthetics.check_once({"id": check_id, "url": "http://127.0.0.1:1/health", "method": "GET", "expect_status": 200, "timeout_ms": 300}, "iad")
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["error"])
        listed = synthetics.list_checks()
        self.assertEqual(listed[0]["uptime_24h"]["checks"], 1)
        self.assertEqual(listed[0]["uptime_24h"]["uptime_pct"], 0.0)

    def test_status_page(self):
        page = synthetics.status_page()
        self.assertIn(page["status"], ("operational", "degraded", "partial_outage"))
        self.assertTrue(page["components"])


class ValueFormatTests(unittest.TestCase):
    """Alert values are numbers; people need units. Both are exposed."""

    @classmethod
    def setUpClass(cls):
        db.reset()

    def test_formats(self):
        from craft.alerting import format_value

        self.assertEqual(format_value(-83146879319.54, "bytes"), "−77.4 GiB")
        self.assertEqual(format_value(1536, "bytes"), "1.5 KiB")
        self.assertEqual(format_value(0.0612, "ratio"), "6.12%")
        self.assertEqual(format_value(0.0612, "percent"), "0.06%")
        self.assertEqual(format_value(4123, "seconds"), "68.7m")
        self.assertEqual(format_value(123456789, ""), "123.46M")
        self.assertEqual(format_value(0, ""), "0")
        self.assertEqual(format_value(None, "bytes"), "—")

    def test_projection_note(self):
        from craft.alerting import value_note

        expr = "predict_linear(node_filesystem_free_bytes[30m], 4*3600) < 0"
        self.assertIsNotNone(value_note(expr, -83146879319.54, "bytes"))
        self.assertIsNone(value_note(expr, 1.2e9, "bytes"), "a positive projection is not news")
        self.assertIsNone(value_note("sum(rate(http_requests_total[5m]))", -1), "not a projection")

    def test_rule_roundtrip_carries_the_unit(self):
        rule_id = alerting.upsert_rule({
            "name": "BytesRule", "expr": "node_filesystem_free_bytes < 1e12",
            "for": 0, "severity": "warning", "format": "bytes",
        })
        try:
            rule = [r for r in alerting.list_rules() if r["id"] == rule_id][0]
            self.assertEqual(rule["format"], "bytes")
            alerting.evaluate()
            rows = [a for a in alerting.list_alerts() if a["name"] == "BytesRule"]
            if rows:  # depends on whether the seeded metric matches right now
                self.assertEqual(rows[0]["format"], "bytes")
                self.assertIn("GiB", rows[0]["value_display"])
            # an unknown unit must not be stored
            alerting.upsert_rule({"name": "BytesRule", "expr": "node_filesystem_free_bytes < 1e12",
                                  "for": 0, "severity": "warning", "format": "furlongs"})
            rule = [r for r in alerting.list_rules() if r["id"] == rule_id][0]
            self.assertEqual(rule["format"], "")
        finally:
            alerting.delete_rule(rule_id)

    def test_default_disk_rule_declares_bytes(self):
        alerting.seed_defaults()
        disk = [r for r in alerting.list_rules() if r["name"] == "DiskWillFillIn4Hours"]
        self.assertTrue(disk)
        self.assertEqual(disk[0]["format"], "bytes")


if __name__ == "__main__":
    unittest.main()
