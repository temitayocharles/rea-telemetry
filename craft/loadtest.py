"""A small load generator, in the spirit of k6 but built in.

Runs real HTTP traffic against a target you own with a bounded number of
virtual users, samples p50/p95/p99 + error rate once per second, and stores the
run so the result can be replayed as a chart.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request

from . import db

MAX_VUS = 20
MAX_DURATION_S = 60
_running: dict[int, dict] = {}
_lock = threading.Lock()


def start(name: str, target: str, vus: int = 5, duration_s: int = 20, ramp: bool = True, method: str = "GET") -> dict:
    vus = max(1, min(int(vus), MAX_VUS))
    duration_s = max(1, min(int(duration_s), MAX_DURATION_S))
    if not target.startswith(("http://", "https://")):
        raise ValueError("target must be an http(s) URL")
    conn = db.connect()
    cur = conn.execute(
        "INSERT INTO loadtests(name, script, status, started_at, created_at) VALUES(?,?,?,?,?)",
        (name, json.dumps({"target": target, "vus": vus, "duration_s": duration_s, "ramp": ramp, "method": method}), "running", db.now(), db.now()),
    )
    test_id = cur.lastrowid
    state = {"latencies": [], "errors": 0, "requests": 0, "stop": False}
    with _lock:
        _running[test_id] = state
    t = threading.Thread(target=_run, args=(test_id, target, vus, duration_s, ramp, method, state), daemon=True, name=f"loadtest-{test_id}")
    t.start()
    return {"id": test_id, "vus": vus, "duration_s": duration_s}


def _run(test_id, target, vus, duration_s, ramp, method, state):
    started = time.time()
    conn = db.connect()

    def worker(index: int):
        while not state["stop"] and time.time() - started < duration_s:
            if ramp and random.random() > (time.time() - started) / max(duration_s, 1) * 1.2:
                time.sleep(0.05)
                continue
            t0 = time.perf_counter()
            ok = True
            try:
                req = urllib.request.Request(target, method=method, headers={"User-Agent": "TelemetryCraft-Load/1.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    resp.read(2048)
                    ok = resp.status < 400
            except Exception:  # noqa: BLE001 - failures are data
                ok = False
            elapsed = (time.perf_counter() - t0) * 1000
            with _lock:
                state["latencies"].append(elapsed)
                state["requests"] += 1
                if not ok:
                    state["errors"] += 1
            time.sleep(random.uniform(0.02, 0.12))

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(vus)]
    for t in threads:
        t.start()
    window: list[float] = []
    errors_window = 0
    requests_window = 0
    while time.time() - started < duration_s:
        time.sleep(1)
        with _lock:
            total = len(state["latencies"])
            batch = state["latencies"][-max(total - requests_window, 0):] if total else []
            requests_window = total
            errors_window = state["errors"]
        pct = lambda q: sorted(batch)[min(len(batch) - 1, int(q * len(batch)))] if batch else 0
        conn.execute(
            "INSERT INTO loadtest_points(test_id, ts, vus, rps, p50, p95, p99, errors) VALUES(?,?,?,?,?,?,?,?)",
            (test_id, db.now(), vus, round(len(batch), 2), round(pct(0.5), 2), round(pct(0.95), 2), round(pct(0.99), 2), round(errors_window / max(requests_window, 1) * 100, 2)),
        )
    state["stop"] = True
    with _lock:
        lat = sorted(state["latencies"])
        n = len(lat)
        summary = {
            "requests": n,
            "errors": state["errors"],
            "error_rate": round(state["errors"] / n * 100, 2) if n else 0,
            "rps": round(n / max(time.time() - started, 0.001), 2),
            "p50": round(lat[int(0.5 * (n - 1))], 2) if n else 0,
            "p95": round(lat[int(0.95 * (n - 1))], 2) if n else 0,
            "p99": round(lat[int(0.99 * (n - 1))], 2) if n else 0,
            "duration_s": round(time.time() - started, 1),
        }
    conn.execute(
        "UPDATE loadtests SET status='complete', finished_at=?, summary=? WHERE id=?",
        (db.now(), json.dumps(summary), test_id),
    )
    with _lock:
        _running.pop(test_id, None)


def list_tests(limit: int = 20) -> list[dict]:
    conn = db.connect()
    out = []
    for r in conn.execute("SELECT * FROM loadtests ORDER BY id DESC LIMIT ?", (limit,)):
        item = dict(r)
        item["config"] = db.jloads(r["script"], {})
        item["summary"] = db.jloads(r["summary"], {})
        item["points"] = [
            dict(p)
            for p in conn.execute("SELECT ts, vus, rps, p50, p95, p99, errors FROM loadtest_points WHERE test_id=? ORDER BY ts", (r["id"],))
        ]
        out.append(item)
    return out


def cancel(test_id: int) -> None:
    with _lock:
        if test_id in _running:
            _running[test_id]["stop"] = True
