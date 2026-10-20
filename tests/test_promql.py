import os
import tempfile
import unittest

os.environ.setdefault("CRAFT_DB", os.path.join(tempfile.mkdtemp(), "promql.db"))

from craft import db, promql, store  # noqa: E402


def reset_db():
    db.reset()
    return db.connect()


class PromQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reset_db()
        base = 1_700_000_000
        with store.MetricWriter() as w:
            for i in range(31):
                ts = base + i * 15
                w.add("http_requests_total", {"service": "api", "status": "200"}, ts, 1000 + i)
                w.add("http_requests_total", {"service": "api", "status": "500"}, ts, 10 + i)
                for le, frac in (("0.1", 0.6), ("0.5", 0.85), ("1", 0.95), ("+Inf", 1.0)):
                    w.add("http_request_duration_seconds_bucket", {"service": "api", "le": le}, ts, 1000 * (i + 1) * frac)
                w.add("http_request_duration_seconds_count", {"service": "api"}, ts, 1000 * (i + 1))
                w.add("app_cpu_usage_ratio", {"service": "api"}, ts, 0.25 + i * 0.01)
                w.add("up", {"job": "api", "instance": "a:1"}, ts, 1)
                w.add("up", {"job": "db", "instance": "b:1"}, ts, 0)
        cls.now = base + 30 * 15
        db.set_meta("_test_now", cls.now)
        # pin the evaluation clock by using explicit timestamps in queries

    def q(self, expr):
        return promql.query_instant(expr, self.now * 1000)

    def test_selector(self):
        res = self.q('up{job="api"}')
        self.assertEqual(len(res["result"]), 1)
        self.assertEqual(float(res["result"][0]["value"][1]), 1.0)

    def test_regex_matcher(self):
        res = self.q('up{job=~"a.*"}')
        self.assertEqual(len(res["result"]), 1)
        res = self.q('up{job!="api"}')
        self.assertEqual(len(res["result"]), 1)

    def test_rate(self):
        res = self.q('rate(http_requests_total{service="api",status="200"}[5m])')
        self.assertEqual(len(res["result"]), 1)
        # counter increments by 1 per 15s sample -> 1/15 per second
        self.assertAlmostEqual(float(res["result"][0]["value"][1]), 1 / 15, places=4)

    def test_sum_by(self):
        res = self.q('sum(rate(http_requests_total[5m])) by (status)')
        self.assertEqual(len(res["result"]), 2)
        labels = {r["metric"]["status"] for r in res["result"]}
        self.assertEqual(labels, {"200", "500"})

    def test_histogram_quantile(self):
        res = self.q('histogram_quantile(0.9, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))')
        value = float(res["result"][0]["value"][1])
        self.assertGreater(value, 0.5)
        self.assertLessEqual(value, 1.0)

    def test_comparison_filter(self):
        res = self.q("app_cpu_usage_ratio > 0.5")
        self.assertEqual(len(res["result"]), 1)
        res = self.q("app_cpu_usage_ratio > 0.9")
        self.assertEqual(len(res["result"]), 0)

    def test_avg_over_time(self):
        res = self.q("avg_over_time(app_cpu_usage_ratio[5m])")
        value = float(res["result"][0]["value"][1])
        self.assertTrue(0.3 < value < 0.5, value)

    def test_range_query(self):
        res = promql.query_range("sum(up)", (self.now - 300) * 1000, self.now * 1000, 60_000)
        self.assertEqual(len(res["result"]), 1)
        self.assertGreaterEqual(len(res["result"][0]["values"]), 5)

    def test_offset(self):
        res = self.q('up{job="api"} offset 2m')
        self.assertEqual(len(res["result"]), 1)

    def test_scalar_and_math(self):
        self.assertAlmostEqual(promql.eval_scalar(promql.parse("2 * 3 + 1"), None), 7.0)

    def test_label_functions(self):
        res = self.q('label_replace(up{job="api"}, "env", "prod", "job", "(.*)")')
        self.assertEqual(res["result"][0]["metric"]["env"], "prod")

    def test_topk_keeps_labels(self):
        res = self.q("topk(1, up)")
        self.assertEqual(len(res["result"]), 1)
        self.assertIn("instance", res["result"][0]["metric"])

    def test_invalid_query(self):
        with self.assertRaises(promql.ParseError):
            promql.parse("sum(rate(http_requests_total[5m]")

    def test_durations(self):
        self.assertEqual(promql.parse_duration("5m"), 300_000)
        self.assertEqual(promql.parse_duration("1h30m"), 5_400_000)
        self.assertEqual(promql.parse_duration("250ms"), 250)

    def test_labels(self):
        self.assertIn("service", promql.label_names())
        self.assertIn("api", promql.label_values("service"))


    # ---- subqueries ----------------------------------------------------
    def test_parse_subquery(self):
        node = promql.parse("rate(sum(rate(http_requests_total[1m]))[5m:15s])")
        self.assertIsInstance(node, promql.Call)
        arg = node.args[0]
        self.assertIsInstance(arg, promql.Subquery)
        self.assertEqual(arg.range_ms, 300_000)
        self.assertEqual(arg.step_ms, 15_000)

    def test_subquery_step_is_optional(self):
        node = promql.parse("max_over_time(up[1h:])")
        self.assertIsNone(node.args[0].step_ms)
        self.assertEqual(node.args[0].range_ms, 3_600_000)

    def test_metric_names_may_contain_colons(self):
        node = promql.parse("node:node_cpu:rate5m")
        self.assertIsInstance(node, promql.Selector)
        self.assertEqual(node.metric, "node:node_cpu:rate5m")

    def test_range_selector_still_requires_a_selector(self):
        with self.assertRaises(promql.ParseError):
            promql.parse("sum(up)[5m]")

    def test_count_over_time_over_a_subquery(self):
        # up is sampled every 15 s, so a 5 minute subquery at 1 minute resolution
        # has six evaluation instants and every one of them has data
        res = self.q("count_over_time(up{job=\"api\"}[5m:1m])")
        self.assertEqual(len(res["result"]), 1)
        self.assertEqual(float(res["result"][0]["value"][1]), 6.0)
        # a window that reaches past the start of the fixture only counts the
        # instants that actually have a sample
        longer = self.q("count_over_time(up{job=\"api\"}[10m:1m])")
        self.assertEqual(float(longer["result"][0]["value"][1]), 8.0)

    def test_default_resolution_matches_an_explicit_step(self):
        implicit = self.q("count_over_time(up{job=\"api\"}[10m:])")
        explicit = self.q("count_over_time(up{job=\"api\"}[10m:1m])")
        self.assertEqual(
            float(implicit["result"][0]["value"][1]),
            float(explicit["result"][0]["value"][1]),
        )

    def test_aggregation_feeds_a_range_function(self):
        # sum(rate(...)) is a rate per series, so max_over_time must pick the largest
        # instantaneous value seen inside the window rather than the value at the instant
        single = self.q("sum(rate(http_requests_total[1m]))")
        windowed = self.q("max_over_time(sum(rate(http_requests_total[1m]))[10m:1m])")
        instant = float(single["result"][0]["value"][1])
        peak = float(windowed["result"][0]["value"][1])
        self.assertGreater(peak, 0.0)
        # counters climb in this fixture, so the newest instant is the peak
        self.assertAlmostEqual(peak, instant, places=6)

    def test_min_over_time_over_a_subquery_sees_the_past(self):
        # the counter rises monotonically, so the oldest point in the window has the
        # lowest rate: this only passes if the subquery actually looks back in time
        peak = self.q("max_over_time(sum(rate(http_requests_total[1m]))[10m:1m])")
        trough = self.q("min_over_time(sum(rate(http_requests_total[1m]))[10m:1m])")
        self.assertLess(float(trough["result"][0]["value"][1]), float(peak["result"][0]["value"][1]))

    def test_subquery_with_offset(self):
        recent = self.q("count_over_time(up{job=\"api\"}[5m:])")
        self.assertEqual(float(recent["result"][0]["value"][1]), 6.0)
        # 30 minutes back is before the fixture starts, so nothing is in range
        old = self.q("count_over_time(up{job=\"api\"}[10m:] offset 30m)")
        self.assertEqual(old["result"], [])

    def test_subquery_inside_a_range_query(self):
        res = promql.query_range("max_over_time(sum(rate(http_requests_total[1m]))[10m:1m])",
                                 (self.now - 120) * 1000, self.now * 1000, 60_000)
        self.assertEqual(len(res["result"]), 1)
        self.assertEqual(len(res["result"][0]["values"]), 3)
        values = [float(v) for _, v in res["result"][0]["values"]]
        self.assertTrue(all(v > 0 for v in values), values)

    def test_subquery_keeps_all_labels(self):
        res = self.q("max_over_time(up[5m:1m])")
        jobs = sorted(s["metric"].get("job") for s in res["result"])
        self.assertEqual(jobs, ["api", "db"])
        api = [s for s in res["result"] if s["metric"]["job"] == "api"][0]
        self.assertEqual(api["metric"]["instance"], "a:1")


if __name__ == "__main__":
    unittest.main()
