"""Incident response: declaration, timeline, roles, status transitions, postmortems."""
from __future__ import annotations

import json
import re

from . import db

STATUSES = ["investigating", "identified", "monitoring", "resolved"]
SEVERITIES = ["SEV1", "SEV2", "SEV3", "SEV4"]

TIMELINE_KINDS = ["declared", "status", "comment", "commander", "escalation", "link", "resolved"]


def _next_reference() -> str:
    conn = db.connect()
    row = conn.execute("SELECT MAX(CAST(SUBSTR(reference, 5) AS INTEGER)) FROM incidents").fetchone()
    n = (row[0] or 0) + 1 if row else 1
    return f"INC-{n:04d}"


def list_incidents(status: str = "") -> list[dict]:
    conn = db.connect()
    sql = "SELECT * FROM incidents"
    args = []
    if status == "open":
        sql += " WHERE resolved_at IS NULL"
    elif status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY CASE severity WHEN 'SEV1' THEN 0 WHEN 'SEV2' THEN 1 WHEN 'SEV3' THEN 2 ELSE 3 END, created_at DESC"
    out = []
    for r in conn.execute(sql, args):
        out.append(_row(r))
    return out


def _row(r) -> dict:
    return {
        "id": r["id"],
        "reference": r["reference"],
        "title": r["title"],
        "severity": r["severity"],
        "status": r["status"],
        "impact": r["impact"],
        "commander": r["commander"],
        "responders": db.jloads(r["responders"], []),
        "services": db.jloads(r["services"], []),
        "created_at": r["created_at"],
        "detected_at": r["detected_at"],
        "resolved_at": r["resolved_at"],
        "summary": r["summary"],
        "postmortem": db.jloads(r["postmortem"], {}),
        "duration_s": (r["resolved_at"] or db.now()) - r["created_at"],
    }


def get_incident(incident_id: int) -> dict | None:
    conn = db.connect()
    r = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    if not r:
        return None
    item = _row(r)
    item["timeline"] = [
        {"id": e["id"], "ts": e["ts"], "kind": e["kind"], "actor": e["actor"], "body": e["body"]}
        for e in conn.execute("SELECT * FROM incident_events WHERE incident_id=? ORDER BY ts, id", (incident_id,))
    ]
    return item


def declare(title: str, severity: str = "SEV2", impact: str = "minor", commander: str = "", services=None, summary: str = "", detected_at=None) -> dict:
    if severity not in SEVERITIES:
        raise ValueError("severity must be one of SEV1..SEV4")
    conn = db.connect()
    now = db.now()
    ref = _next_reference()
    cur = conn.execute(
        "INSERT INTO incidents(reference, title, severity, status, impact, commander, responders, services, created_at, detected_at, summary) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ref, title, severity, "investigating", impact, commander, json.dumps([commander] if commander else []), json.dumps(services or []), now, detected_at or now, summary),
    )
    incident_id = cur.lastrowid
    add_event(incident_id, "declared", commander or "system", f"Incident declared at {severity}: {title}")
    return {"id": incident_id, "reference": ref}


def declare_from_alert(alert_id: int, commander: str = "") -> dict:
    conn = db.connect()
    alert = conn.execute("SELECT * FROM alert_instances WHERE id=?", (alert_id,)).fetchone()
    if not alert:
        raise ValueError("alert not found")
    labels = db.jloads(alert["labels"], {})
    services = [labels.get("service")] if labels.get("service") else []
    sev = "SEV1" if alert["severity"] == "critical" and labels.get("tier") == "1" else ("SEV2" if alert["severity"] == "critical" else "SEV3")
    return declare(alert["name"], sev, "major" if sev in ("SEV1", "SEV2") else "minor", commander, services, alert["summary"], detected_at=alert["active_since"])


def update_status(incident_id: int, status: str, actor: str = "", body: str = "") -> None:
    if status not in STATUSES:
        raise ValueError("invalid status")
    conn = db.connect()
    resolved = db.now() if status == "resolved" else None
    conn.execute("UPDATE incidents SET status=?, resolved_at=COALESCE(?, resolved_at) WHERE id=?", (status, resolved, incident_id))
    add_event(incident_id, "resolved" if status == "resolved" else "status", actor or "system", body or f"Status changed to {status}")


def assign_commander(incident_id: int, commander: str, actor: str = "") -> None:
    conn = db.connect()
    conn.execute("UPDATE incidents SET commander=? WHERE id=?", (commander, incident_id))
    add_event(incident_id, "commander", actor or commander, f"{commander} is incident commander")


def add_responder(incident_id: int, person: str) -> None:
    conn = db.connect()
    r = conn.execute("SELECT responders FROM incidents WHERE id=?", (incident_id,)).fetchone()
    people = db.jloads(r["responders"], []) if r else []
    if person and person not in people:
        people.append(person)
        conn.execute("UPDATE incidents SET responders=? WHERE id=?", (json.dumps(people), incident_id))
        add_event(incident_id, "escalation", person, f"{person} joined the response")


def add_event(incident_id: int, kind: str, actor: str, body: str) -> None:
    db.connect().execute(
        "INSERT INTO incident_events(incident_id, ts, kind, actor, body) VALUES(?,?,?,?,?)",
        (incident_id, db.now(), kind, actor, body),
    )


def save_postmortem(incident_id: int, doc: dict) -> None:
    conn = db.connect()
    conn.execute("UPDATE incidents SET postmortem=? WHERE id=?", (json.dumps(doc), incident_id))
    add_event(incident_id, "comment", doc.get("author", "system"), "Postmortem draft updated")


def open_incidents() -> list[dict]:
    return list_incidents("open")


def resolve_all() -> int:
    conn = db.connect()
    rows = conn.execute("SELECT id FROM incidents WHERE resolved_at IS NULL").fetchall()
    for r in rows:
        update_status(r["id"], "resolved", "system", "Bulk resolve")
    return len(rows)
