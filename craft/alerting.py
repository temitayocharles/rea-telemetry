"""Alert evaluation, grouping, silencing and routing.

The rules you write in PromQL here are the same rules Prometheus/Alertmanager
would take. Evaluation adds `for` semantics (pending -> firing), silences,
grouping and delivery to webhook/Slack/PagerDuty-style receivers.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

from . import db, promql

_last_eval = {"at": 0, "checked": 0, "errors": []}
_eval_lock = threading.Lock()


def default_receivers() -> list[dict]:
    return [
        {"name": "slack-oncall", "kind": "slack", "url": "", "enabled": True},
        {"name": "pagerduty-primary", "kind": "pagerduty", "url": "", "enabled": True},
        {"name": "email-sre", "kind": "email", "url": "", "enabled": False},
        {"name": "webhook-audit", "kind": "webhook", "url": "", "enabled": True},
    ]


def receivers() -> list[dict]:
    return db.get_meta("receivers", None) or default_receivers()


def save_receivers(items: list[dict]) -> None:
    db.set_meta("receivers", items)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def list_rules() -> list[dict]:
    rows = db.connect().execute("SELECT * FROM rules ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "expr": r["expr"],
            "for": r["for_s"],
            "severity": r["severity"],
            "summary": r["summary"],
            "runbook": r["runbook"],
            "group": r["group_name"],
            "enabled": bool(r["enabled"]),
            "labels": db.jloads(r["labels"], {}),
        }
        for r in rows
    ]


def upsert_rule(rule: dict) -> int:
    conn = db.connect()
    name = rule["name"].strip()
    if not name:
        raise ValueError("rule name required")
    promql.parse(rule["expr"])
    existing = conn.execute("SELECT id FROM rules WHERE name=?", (name,)).fetchone()
    values = (
        rule["expr"],
        int(rule.get("for", 300)),
        rule.get("severity", "warning"),
        rule.get("summary", ""),
        rule.get("runbook", ""),
        rule.get("group", "default"),
        1 if rule.get("enabled", True) else 0,
        json.dumps(rule.get("labels", {})),
    )
    if existing:
        conn.execute(
            "UPDATE rules SET expr=?, for_s=?, severity=?, summary=?, runbook=?, group_name=?, enabled=?, labels=? WHERE id=?",
            (*values, existing["id"]),
        )
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO rules(name, expr, for_s, severity, summary, runbook, group_name, enabled, labels) VALUES(?,?,?,?,?,?,?,?,?)",
        (name, *values),
    )
    return cur.lastrowid


def delete_rule(rule_id: int) -> None:
    conn = db.connect()
    conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.execute("DELETE FROM alert_instances WHERE rule_id=? AND state='inactive'", (rule_id,))


def rule_preview(expr: str) -> dict:
    try:
        res = promql.query_instant(expr, db.now() * 1000)
        return {"ok": True, "result": res}
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# Silences
# --------------------------------------------------------------------------
def list_silences(active_only=False) -> list[dict]:
    conn = db.connect()
    rows = conn.execute("SELECT * FROM silences ORDER BY ends_at DESC").fetchall()
    now = db.now()
    out = []
    for r in rows:
        state = "expired" if r["ends_at"] < now else ("pending" if r["starts_at"] > now else "active")
        if active_only and state != "active":
            continue
        out.append(
            {
                "id": r["id"],
                "fingerprint": r["fingerprint"],
                "matchers": db.jloads(r["matchers"], []),
                "starts_at": r["starts_at"],
                "ends_at": r["ends_at"],
                "created_by": r["created_by"],
                "comment": r["comment"],
                "state": state,
            }
        )
    return out


def create_silence(matchers: list[dict], duration_s: int, created_by: str, comment: str = "") -> dict:
    import hashlib

    conn = db.connect()
    now = db.now()
    fp = hashlib.blake2b(
        (json.dumps(matchers, sort_keys=True) + str(now)).encode(), digest_size=8
    ).hexdigest()
    conn.execute(
        "INSERT INTO silences(fingerprint, matchers, starts_at, ends_at, created_by, comment) VALUES(?,?,?,?,?,?)",
        (fp, json.dumps(matchers), now, now + int(duration_s), created_by, comment),
    )
    return {"fingerprint": fp, "starts_at": now, "ends_at": now + int(duration_s)}


def _silenced(labels: dict) -> bool:
    now = db.now()
    for s in list_silences(active_only=True):
        if s["ends_at"] < now:
            continue
        if all(_match(labels.get(m["name"], ""), m.get("op", "="), m.get("value", "")) for m in s["matchers"]):
            return True
    return False


def _match(actual: str, op: str, val: str) -> bool:
    if op == "=":
        return actual == val
    if op == "!=":
        return actual != val
    if op == "=~":
        return bool(re.fullmatch(val, actual))
    if op == "!~":
        return not re.fullmatch(val, actual)
    return False


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
def evaluate(now: int | None = None) -> dict:
    now = now or db.now()
    with _eval_lock:
        conn = db.connect()
        rules = conn.execute("SELECT * FROM rules WHERE enabled=1").fetchall()
        seen = set()
        errors = []
        pending, firing, resolved = 0, 0, 0
        notified = 0
        for rule in rules:
            try:
                res = promql.query_instant(rule["expr"], now * 1000)
            except Exception as exc:  # noqa: BLE001
                errors.append({"rule": rule["name"], "error": str(exc)})
                continue
            results = res.get("result", [])
            if res.get("resultType") == "scalar":
                results = [{"metric": {}, "value": res["result"]}]
            for series in results:
                value = float(series["value"][1])
                labels = {k: v for k, v in series["metric"].items() if k != "__name__"}
                labels.update(db.jloads(rule["labels"], {}))
                fp = _alert_fp(rule["name"], labels)
                seen.add(fp)
                existing = conn.execute("SELECT * FROM alert_instances WHERE fingerprint=?", (fp,)).fetchone()
                if existing is None:
                    conn.execute(
                        """INSERT INTO alert_instances(fingerprint, rule_id, name, severity, summary, labels, value, expr, state, active_since, last_eval)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (fp, rule["id"], rule["name"], rule["severity"], rule["summary"] or rule["name"], json.dumps(labels), value, rule["expr"], "pending", now, now),
                    )
                    existing = conn.execute("SELECT * FROM alert_instances WHERE fingerprint=?", (fp,)).fetchone()
                state = existing["state"]
                if state == "resolved":
                    conn.execute(
                        "UPDATE alert_instances SET state='pending', active_since=?, resolved_at=NULL, ends_at=NULL, acked_by=NULL, acked_at=NULL WHERE id=?",
                        (now, existing["id"]),
                    )
                    state = "pending"
                if state == "pending":
                    if rule["for_s"] == 0 or now - existing["active_since"] >= rule["for_s"]:
                        state = "firing"
                        conn.execute("UPDATE alert_instances SET state='firing' WHERE id=?", (existing["id"],))
                        _notify(rule, existing, labels, value)
                        notified += 1
                    else:
                        pending += 1
                elif state == "firing":
                    firing += 1
                conn.execute("UPDATE alert_instances SET value=?, last_eval=? WHERE id=?", (value, now, existing["id"]))
        for row in conn.execute("SELECT * FROM alert_instances WHERE state IN ('firing','pending')").fetchall():
            if row["fingerprint"] not in seen:
                conn.execute("UPDATE alert_instances SET state='resolved', resolved_at=?, ends_at=? WHERE id=?", (now, now, row["id"]))
                resolved += 1
        _last_eval.update(at=now, checked=len(rules), errors=errors)
        return {"checked": len(rules), "pending": pending, "firing": firing, "resolved": resolved, "notified": notified, "errors": errors}


def last_evaluation() -> dict:
    return dict(_last_eval)


def _alert_fp(name: str, labels: dict) -> str:
    import hashlib

    return hashlib.blake2b((name + json.dumps(labels, sort_keys=True)).encode(), digest_size=10).hexdigest()


def _notify(rule, instance, labels, value) -> None:
    title = f"[{rule['severity'].upper()}] {rule['name']}"
    body = rule["summary"] or rule["name"]
    group = rule["group_name"] or "default"
    delivered = []
    for recv in receivers():
        if not recv.get("enabled", True):
            continue
        if _silenced({**labels, "alertname": rule["name"], "severity": rule["severity"], "group": group}):
            status = "silenced"
        else:
            status = "delivered"
            url = recv.get("url")
            if url and recv.get("kind") == "webhook":
                status = _post(url, {"alert": rule["name"], "severity": rule["severity"], "summary": body, "labels": labels, "value": value})
        db.connect().execute(
            "INSERT INTO notifications(ts, receiver, kind, title, body, alert_name, status) VALUES(?,?,?,?,?,?,?)",
            (db.now(), recv["name"], recv["kind"], title, body, rule["name"], status),
        )
        delivered.append(recv["name"])
    return delivered


def _post(url: str, payload: dict) -> str:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return "delivered" if 200 <= resp.status < 300 else f"failed {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {str(exc)[:80]}"


# manual notification used by the UI for a test fire
def test_route(receiver: str) -> dict:
    recv = next((r for r in receivers() if r["name"] == receiver), None)
    if not recv:
        raise ValueError("unknown receiver")
    db.connect().execute(
        "INSERT INTO notifications(ts, receiver, kind, title, body, alert_name, status) VALUES(?,?,?,?,?,?,?)",
        (db.now(), recv["name"], recv["kind"], "Test notification", "Delivery test from TelemetryCraft", "test", "delivered"),
    )
    return {"receiver": recv["name"], "status": "delivered"}


def list_alerts(state: str = "") -> list[dict]:
    conn = db.connect()
    sql = "SELECT * FROM alert_instances"
    args = []
    if state:
        sql += " WHERE state=?"
        args.append(state)
    sql += " ORDER BY CASE state WHEN 'firing' THEN 0 WHEN 'pending' THEN 1 WHEN 'resolved' THEN 2 ELSE 3 END, last_eval DESC"
    out = []
    for r in conn.execute(sql, args):
        labels = db.jloads(r["labels"], {})
        out.append(
            {
                "id": r["id"],
                "name": r["name"],
                "severity": r["severity"],
                "summary": r["summary"],
                "labels": labels,
                "value": r["value"],
                "expr": r["expr"],
                "state": r["state"],
                "active_since": r["active_since"],
                "last_eval": r["last_eval"],
                "acked_by": r["acked_by"],
                "silenced": _silenced({**labels, "alertname": r["name"], "severity": r["severity"]}),
                "duration_s": (r["resolved_at"] or db.now()) - r["active_since"],
            }
        )
    return out


def ack(alert_id: int, who: str) -> None:
    db.connect().execute("UPDATE alert_instances SET acked_by=?, acked_at=? WHERE id=?", (who, db.now(), alert_id))


def list_notifications(limit=100) -> list[dict]:
    rows = db.connect().execute("SELECT * FROM notifications ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# On-call
# --------------------------------------------------------------------------
def who_is_on_call(at: int | None = None) -> list[dict]:
    at = at or db.now()
    conn = db.connect()
    out = []
    for s in conn.execute("SELECT * FROM schedules ORDER BY name"):
        members = db.jloads(s["members"], [])
        if not members:
            continue
        rotation = s["rotation"]
        if rotation == "weekly":
            days = at // 86400
            idx = (days // 7) % len(members)
            next_handoff = (days // 7 + 1) * 7 * 86400 + s["handoff_hour"] * 3600
        elif rotation == "daily":
            days = at // 86400
            idx = days % len(members)
            next_handoff = (days + 1) * 86400 + s["handoff_hour"] * 3600
        else:  # follow_the_sun: 8h blocks
            blocks = at // (8 * 3600)
            idx = blocks % len(members)
            next_handoff = (blocks + 1) * 8 * 3600
        out.append(
            {
                "schedule": s["name"],
                "rotation": rotation,
                "on_call": members[idx],
                "next_handoff": next_handoff,
                "members": members,
            }
        )
    return out


def seed_defaults() -> None:
    conn = db.connect()
    if conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0:
        for rule in DEFAULT_RULES:
            upsert_rule(rule)
    if conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO schedules(name, rotation, members, handoff_hour) VALUES(?,?,?,?)",
            [
                ("platform-primary", "weekly", json.dumps(["Ade Okonkwo", "Priya Raman", "Marta Silva", "Jonas Weber"]), 9),
                ("payments-primary", "daily", json.dumps(["Chen Wu", "Sofia Marino", "Ibrahim Khan"]), 10),
                ("database-escalation", "follow_the_sun", json.dumps(["Nadia Haddad", "Tom Becker", "Yuki Sato"]), 8),
            ],
        )


DEFAULT_RULES = [
    {
        "name": "HighErrorRate",
        "expr": 'sum(rate(http_requests_total{status=~"5.."}[5m])) by (service) / sum(rate(http_requests_total[5m])) by (service) > 0.02',
        "for": 300,
        "severity": "critical",
        "summary": "5xx error ratio above 2% for 5 minutes",
        "runbook": "https://runbooks.internal/high-error-rate",
        "group": "http",
    },
    {
        "name": "LatencyP99High",
        "expr": "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service)) > 0.8",
        "for": 600,
        "severity": "warning",
        "summary": "p99 latency above 800ms for 10 minutes",
        "runbook": "https://runbooks.internal/latency",
        "group": "http",
    },
    {
        "name": "PodCrashLooping",
        "expr": "max_over_time(kube_pod_container_status_restarts_total[10m]) - min_over_time(kube_pod_container_status_restarts_total[10m]) > 3",
        "for": 300,
        "severity": "critical",
        "summary": "Container restarting repeatedly",
        "group": "kubernetes",
    },
    {
        "name": "DiskWillFillIn4Hours",
        "expr": "predict_linear(node_filesystem_free_bytes[30m], 4*3600) < 0",
        "for": 900,
        "severity": "warning",
        "summary": "Filesystem projected to fill within 4 hours",
        "group": "host",
    },
    {
        "name": "QueueBacklog",
        "expr": "sum(queue_messages_ready) by (queue) > 5000",
        "for": 600,
        "severity": "warning",
        "summary": "Queue backlog above threshold",
        "group": "messaging",
    },
    {
        "name": "DatabaseConnectionsSaturated",
        "expr": "pg_stat_activity_count / pg_settings_max_connections > 0.85",
        "for": 300,
        "severity": "critical",
        "summary": "Postgres connection pool nearly exhausted",
        "group": "database",
    },
]
