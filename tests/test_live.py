"""The demo environment has to stay alive: the collector appends new points and
rebases after a gap, so charts never go to zero five minutes after boot."""
import os
import tempfile
import unittest

os.environ.setdefault("CRAFT_DB", os.path.join(tempfile.mkdtemp(), "live.db"))

from craft import db, seed, store  # noqa: E402


def write(metric, ts, value, labels=None, stype="counter"):
    with store.MetricWriter() as w:
        w.add(metric, labels or {"job": "demo"}, ts, value, stype)


class LiveTickTests(unittest.TestCase):
    def setUp(self):
        db.reset()
        db.set_meta("seed_version", 1)

    def test_appends_one_point_per_series(self):
        now = db.now()
        write("live_counter_total", now - 60, 100)
        write("live_counter_total", now, 130)
        write("live_gauge", now - 60, 0.5, stype="gauge")
        write("live_gauge", now, 0.52, stype="gauge")
        res = seed.live_tick(now + 90)
        self.assertEqual(res["samples"], 2)
        self.assertEqual(res["rebased_s"], 0)
        rows = db.connect().execute(
            "SELECT s.metric, x.ts, x.value FROM samples x JOIN series s ON s.id=x.series_id "
            "WHERE x.ts = ? ORDER BY s.metric", (now + 90,)
        ).fetchall()
        self.assertEqual(len(rows), 2)
        counter = [r for r in rows if r["metric"] == "live_counter_total"][0]
        # extrapolated at roughly the same rate per second, with jitter
        self.assertGreater(counter["value"], 130)
        self.assertLess(counter["value"], 130 + 60)
        gauge = [r for r in rows if r["metric"] == "live_gauge"][0]
        self.assertGreater(gauge["value"], 0.45)
        self.assertLess(gauge["value"], 0.55)

    def test_clamps_nonsense_values(self):
        now = db.now()
        write("app_cpu_usage_ratio", now - 60, 0.985)
        write("app_cpu_usage_ratio", now, 0.99, stype="gauge")
        write("queue_messages_ready", now - 60, 2, stype="gauge")
        write("queue_messages_ready", now, 0, stype="gauge")
        seed.live_tick(now + 60)
        ratios = db.connect().execute(
            "SELECT x.value FROM samples x JOIN series s ON s.id=x.series_id "
            "WHERE s.metric='app_cpu_usage_ratio' ORDER BY x.ts DESC LIMIT 1"
        ).fetchone()[0]
        queue = db.connect().execute(
            "SELECT x.value FROM samples x JOIN series s ON s.id=x.series_id "
            "WHERE s.metric='queue_messages_ready' ORDER BY x.ts DESC LIMIT 1"
        ).fetchone()[0]
        self.assertLessEqual(ratios, 0.99)
        self.assertGreaterEqual(queue, 0)

    def test_counter_continues_when_previous_point_is_old(self):
        # regression: a single point inside a 4 minute window used to freeze the counter forever
        now = db.now()
        write("slow_counter_total", now - 400, 1000)
        write("slow_counter_total", now - 200, 1100)
        seed.live_tick(now + 60)
        points = db.connect().execute(
            "SELECT x.ts, x.value FROM samples x JOIN series s ON s.id=x.series_id "
            "ORDER BY x.ts"
        ).fetchall()
        self.assertEqual(len(points), 3)
        self.assertGreater(points[-1]["value"], 1100)
        self.assertLess(points[-1]["value"], 1200)

    def test_rebases_after_a_gap(self):
        now = db.now()
        write("gap_counter_total", now - 7200, 10)
        write("gap_counter_total", now - 7140, 40)
        res = seed.live_tick(now)
        self.assertGreater(res["rebased_s"], 7000)
        latest = db.connect().execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        self.assertLessEqual(abs(now - latest), 5)
        # after rebasing there must be at least two points inside a five minute window
        recent = db.connect().execute("SELECT COUNT(*) FROM samples WHERE ts >= ?", (now - 300,)).fetchone()[0]
        self.assertGreaterEqual(recent, 2)
        res = db.connect().execute("SELECT x.value FROM samples x JOIN series s ON s.id=x.series_id ORDER BY x.ts DESC LIMIT 1").fetchone()[0]
        self.assertGreater(res, 40)

    def test_skips_when_fresh_or_disabled(self):
        now = db.now()
        write("fresh_total", now - 5, 1)
        self.assertEqual(seed.live_tick(now)["skipped"], "fresh")
        os.environ["CRAFT_LIVE"] = "0"
        try:
            self.assertEqual(seed.live_tick(now + 600)["skipped"], "disabled")
        finally:
            del os.environ["CRAFT_LIVE"]

    def test_no_demo_data_is_a_noop(self):
        db.reset()
        self.assertEqual(seed.live_tick()["skipped"], "no demo data")


if __name__ == "__main__":
    unittest.main()
