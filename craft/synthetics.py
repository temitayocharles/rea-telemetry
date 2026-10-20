"""Synthetic monitoring: HTTP checks from multiple regions, uptime & status page."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import db

_thread: threading.Thread | None = None
_stop = threading.Event()
_state = {"runs": 0, "failures": 0, "last_run": 0}

REGIONS = ["iad", "fra", "sin", "sfo"]


def check_once(check: dict, region: str = "iad") -> dict:
    started = time.time()
    result = {"check_id": check["id"], "ts": int(started), "ok": False, "status_code": None, "latency_ms": None, "region": region, "error": None}
    timeout = (check.get("timeout_ms") or 5000) / 1000.0
    try:
        req = urllib.request.Request(check["url"], method=check.get("method", "GET"), headers={"User-Agent": "TelemetryCraft-Synthetic/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
            result["status_code"] = resp.status
            result["latency_ms"] = round((time.time() - started) * 1000, 1)
            ok = resp.status == check.get("expect_status", 200)
            if check.get("expect_body") and check["expect_body"] not in body:
                ok = False
                result["error"] = "body assertion failed"
            result["ok"] = ok
    except urllib.error.HTTPError as exc:
        result["status_code"] = exc.code
        result["latency_ms"] = round((time.time() - started) * 1000, 1)
        result["error"] = f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - reported in the result row
        result["latency_ms"] = round((time.time() - started) * 1000, 1)
        result["error"] = str(exc)[:200]
    conn = db.connect()
    conn.execute(
        "INSERT INTO check_results(check_id, ts, ok, status_code, latency_ms, region, error) VALUES(?,?,?,?,?,?,?)",
        (result["check_id"], result["ts"], 1 if result["ok"] else 0, result["status_code"], result["latency_ms"], region, result["error"]),
    )
    return result


def run_due() -> dict:
    conn = db.connect()
    now = db.now()
    checks = conn.execute("SELECT * FROM checks WHERE enabled=1").fetchall()
    ran = 0
    failures = 0
    for c in checks:
        last = conn.execute("SELECT MAX(ts) FROM check_results WHERE check_id=?", (c["id"],)).fetchone()[0]
        if last and now - last < (c["interval_s"] or 60):
            continue
        for region in db.jloads(c["regions"], []) or ["iad"]:
            res = check_once(dict(c), region)
            ran += 1
            if not res["ok"]:
                failures += 1
    _state.update(runs=ran, failures=failures, last_run=now)
    return {"ran": ran, "failures": failures}


def uptime(check_id: int, hours: int = 24) -> dict:
    conn = db.connect()
    since = db.now() - hours * 3600
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(ok) ok, AVG(latency_ms) lat FROM check_results WHERE check_id=? AND ts>=?", (check_id, since)
    ).fetchone()
    n = row["n"] or 0
    return {
        "checks": n,
        "uptime_pct": round((row["ok"] or 0) / n * 100, 3) if n else 100.0,
        "avg_latency_ms": round(row["lat"], 1) if row["lat"] else None,
        "failures": n - (row["ok"] or 0),
    }


def list_checks() -> list[dict]:
    conn = db.connect()
    out = []
    for r in conn.execute("SELECT * FROM checks ORDER BY id"):
        item = dict(r)
        item["regions"] = db.jloads(r["regions"], [])
        item["enabled"] = bool(r["enabled"])
        item["public"] = bool(r["public"])
        item["uptime_24h"] = uptime(r["id"], 24)
        last = conn.execute("SELECT * FROM check_results WHERE check_id=? ORDER BY ts DESC LIMIT 1", (r["id"],)).fetchone()
        item["last"] = dict(last) if last else None
        item["history"] = [
            {"ts": row["ts"], "ok": row["ok"], "latency_ms": row["latency_ms"], "region": row["region"], "status_code": row["status_code"], "error": row["error"]}
            for row in conn.execute(
                "SELECT ts, ok, latency_ms, region, status_code, error FROM check_results WHERE check_id=? ORDER BY ts DESC LIMIT 60", (r["id"],)
            )
        ][::-1]
        out.append(item)
    return out


def upsert_check(check: dict) -> int:
    conn = db.connect()
    name = check["name"].strip()
    values = (
        check.get("kind", "http"),
        check["url"],
        check.get("method", "GET"),
        int(check.get("expect_status", 200)),
        check.get("expect_body", ""),
        int(check.get("interval_s", 60)),
        int(check.get("timeout_ms", 5000)),
        json.dumps(check.get("regions", ["iad"])),
        1 if check.get("enabled", True) else 0,
        check.get("group", "Core"),
        1 if check.get("public", True) else 0,
    )
    row = conn.execute("SELECT id FROM checks WHERE name=?", (name,)).fetchone()
    if row:
        conn.execute(
            "UPDATE checks SET kind=?, url=?, method=?, expect_status=?, expect_body=?, interval_s=?, timeout_ms=?, regions=?, enabled=?, group_name=?, public=? WHERE id=?",
            (*values, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO checks(name, kind, url, method, expect_status, expect_body, interval_s, timeout_ms, regions, enabled, group_name, public) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, *values),
    )
    return cur.lastrowid


def delete_check(check_id: int) -> None:
    conn = db.connect()
    conn.execute("DELETE FROM checks WHERE id=?", (check_id,))
    conn.execute("DELETE FROM check_results WHERE check_id=?", (check_id,))


def status_page() -> dict:
    conn = db.connect()
    checks = conn.execute("SELECT * FROM checks WHERE enabled=1 AND public=1 ORDER BY group_name, name").fetchall()
    groups: dict[str, list] = {}
    for c in checks:
        up = uptime(c["id"], 24)
        last = conn.execute("SELECT * FROM check_results WHERE check_id=? ORDER BY ts DESC LIMIT 1", (c["id"],)).fetchone()
        days = _daily_bars(c["id"], 30)
        groups.setdefault(c["group_name"] or "Core", []).append(
            {
                "name": c["name"],
                "url": c["url"],
                "description": c["name"],
                "uptime_pct": up["uptime_pct"],
                "latency_ms": last["latency_ms"] if last else None,
                "ok": bool(last["ok"]) if last else None,
                "days": days,
            }
        )
    active = conn.execute("SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY created_at DESC").fetchall()
    components = []
    all_ok = True
    for g, items in groups.items():
        ok = all(i["ok"] for i in items if i["ok"] is not None)
        all_ok = all_ok and ok
        components.append({"group": g, "checks": items, "ok": ok, "uptime_pct": round(sum(i["uptime_pct"] for i in items) / len(items), 3) if items else 100})
    return {
        "status": "operational" if all_ok and not active else ("degraded" if not active else "partial_outage"),
        "components": components,
        "incidents": [
            {"reference": i["reference"], "title": i["title"], "status": i["status"], "severity": i["severity"], "created_at": i["created_at"], "summary": i["summary"]}
            for i in active
        ],
        "generated_at": db.now(),
    }


def _daily_bars(check_id: int, days: int) -> list[dict]:
    conn = db.connect()
    out = []
    now = db.now()
    for d in range(days - 1, -1, -1):
        end = now - d * 86400
        start = end - 86400
        row = conn.execute(
            "SELECT COUNT(*) n, SUM(ok) ok FROM check_results WHERE check_id=? AND ts>=? AND ts<?", (check_id, start, end)
        ).fetchone()
        if not row["n"]:
            out.append({"day": end, "status": "nodata", "uptime_pct": None})
            continue
        pct = (row["ok"] or 0) / row["n"] * 100
        state = "up" if pct >= 99.5 else ("degraded" if pct >= 95 else "down")
        out.append({"day": end, "status": state, "uptime_pct": round(pct, 2)})
    return out


def start_worker(interval: float = 5.0) -> None:
    global _thread

    def loop():
        while not _stop.wait(interval):
            try:
                run_due()
            except Exception:  # noqa: BLE001 - never kill the collector loop
                pass

    _thread = threading.Thread(target=loop, name="craft-synthetics", daemon=True)
    _thread.start()


def stop_worker() -> None:
    _stop.set()
