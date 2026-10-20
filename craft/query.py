"""Log search (LogQL subset), trace search, and cross-signal search."""
from __future__ import annotations

import json
import re

from . import db


class QueryError(ValueError):
    pass


# --------------------------------------------------------------------------
# LogQL
# --------------------------------------------------------------------------
STREAM_RE = re.compile(r"^\s*\{([^}]*)\}(.*)$", re.S)
STREAM_MATCH = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*(\"(?:\\.|[^\"\\])*\"|`[^`]*`)")
AGG_WRAPPER = re.compile(
    r"^\s*(?:(sum|avg|max|min|count)\s*(?:by\s*\(([^)]*)\))?\s*\()?\s*count_over_time\s*\((.+)\[(\d+[smhdw])\]\)\s*\)?\s*$", re.S
)


def parse_stream_query(q: str) -> dict:
    q = q.strip()
    if not q:
        return {"matchers": [], "line_filters": [], "label_filters": [], "parsers": []}
    m = STREAM_RE.match(q)
    if not m:
        raise QueryError("a log query starts with a stream selector, e.g. {service=\"api\"}")
    inner, rest = m.group(1), m.group(2)
    matchers = []
    for mm in STREAM_MATCH.finditer(inner):
        val = mm.group(3)
        val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\") if val[0] == '"' else val[1:-1]
        matchers.append([mm.group(1), mm.group(2), val])
    line_filters, label_filters, parsers = [], [], []
    tokens = _split_pipes(rest)
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok in ("json", "logfmt", "pattern", "regexp"):
            parsers.append(tok)
            continue
        if tok.startswith("line_format") or tok.startswith("label_format"):
            continue
        lm = re.match(r"^(?:\|=\s*|\|~\s*|!=\s*|!~\s*)(.*)$", tok, re.S)
        if lm:
            op = tok[:2]
            if tok.startswith("|="):
                op = "|="
            elif tok.startswith("|~"):
                op = "|~"
            elif tok.startswith("!="):
                op = "!="
            else:
                op = "!~"
            val = tok[2:].strip()
            if val.startswith('"') or val.startswith("`"):
                val = _unquote(val)
                line_filters.append([op, val, False])
            continue
        km = re.match(r"^([a-zA-Z_][a-zA-Z0-9_.]*)\s*(=~|!~|!=|=)\s*(.+)$", tok)
        if km:
            label_filters.append([km.group(1), km.group(2), _unquote(km.group(3).strip())])
    return {"matchers": matchers, "line_filters": line_filters, "label_filters": label_filters, "parsers": parsers}


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"`":
        return v[1:-1]
    return v


def _split_pipes(rest: str) -> list[str]:
    """Split on | that are not inside quotes or parens."""
    out, buf, quote, depth = [], [], None, 0
    for ch in rest:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"`":
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "|" and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _match_stream(labels: dict, matchers) -> bool:
    for name, op, val in matchers:
        actual = labels.get(name, "")
        if op == "=" and actual != val:
            return False
        if op == "!=" and actual == val:
            return False
        if op == "=~" and not re.fullmatch(val, actual):
            return False
        if op == "!~" and re.fullmatch(val, actual):
            return False
    return True


def query_logs(q: str, start: int, end: int, limit: int = 200, direction: str = "backward") -> dict:
    parsed = parse_stream_query(q)
    conn = db.connect()
    where = ["ts>=?", "ts<=?"]
    args: list = [start, end]
    for name, op, val in parsed["matchers"]:
        if name in ("service", "level"):
            col = name
            if op == "=":
                where.append(f"{col}=?")
                args.append(val)
            elif op == "!=":
                where.append(f"{col}!=?")
                args.append(val)
            elif op == "=~":
                where.append(f"{col} REGEXP ?")
                args.append(val)
            elif op == "!~":
                where.append(f"NOT ({col} REGEXP ?)")
                args.append(val)
    order = "DESC" if direction == "backward" else "ASC"
    sql = f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY ts {order} LIMIT ?"
    rows = conn.execute(sql, (*args, min(limit * 4 + 50, 20000))).fetchall()
    out = []
    for r in rows:
        labels = db.jloads(r["labels"], {})
        if not _match_stream(labels, [m for m in parsed["matchers"] if m[0] not in ("service", "level")]):
            continue
        attrs = db.jloads(r["attrs"], {})
        body = r["body"]
        ok = True
        for op, val, _ in parsed["line_filters"]:
            if op == "|=" and val.lower() not in body.lower():
                ok = False
            elif op == "!=" and val.lower() in body.lower():
                ok = False
            elif op == "|~" and not re.search(val, body):
                ok = False
            elif op == "!~" and re.search(val, body):
                ok = False
        if not ok:
            continue
        for name, op, val in parsed["label_filters"]:
            actual = str(attrs.get(name, labels.get(name, r[name] if name in ("service", "level") else "")))
            if op == "=" and actual != val:
                ok = False
            elif op == "!=" and actual == val:
                ok = False
            elif op == "=~" and not re.fullmatch(val, actual):
                ok = False
            elif op == "!~" and re.fullmatch(val, actual):
                ok = False
            if not ok:
                break
        if not ok:
            continue
        out.append(
            {
                "ts": r["ts"],
                "service": r["service"],
                "level": r["level"] or "info",
                "body": body,
                "labels": labels,
                "attrs": attrs,
                "trace_id": r["trace_id"],
            }
        )
        if len(out) >= limit:
            break
    return {"entries": out, "count": len(out), "parsed": parsed}


def log_volume(q: str, start: int, end: int, step: int) -> dict:
    """Time series of log counts. Accepts count_over_time({...}[5m]) or a bare selector."""
    m = AGG_WRAPPER.match(q)
    group_by = []
    if m and m.group(3):
        inner = m.group(3)
        if m.group(2):
            group_by = [x.strip() for x in m.group(2).split(",") if x.strip()]
        res = query_logs(inner, start, end, limit=200000, direction="backward")
    else:
        res = query_logs(q, start, end, limit=200000, direction="backward")
    step = max(int(step), 1)
    buckets: dict[tuple, dict[int, int]] = {}
    total: dict[int, int] = {}
    for e in res["entries"]:
        b = start + ((e["ts"] - start) // step) * step
        total[b] = total.get(b, 0) + 1
        for g in group_by:
            key = (g, str(e.get(g) or e["labels"].get(g, "") or e["attrs"].get(g, "")))
            buckets.setdefault(key, {})
            buckets[key][b] = buckets[key].get(b, 0) + 1
    ticks = list(range(start, end + 1, step))
    series = []
    if group_by:
        for (g, val), counts in buckets.items():
            series.append({"label": f"{g}={val}", "values": [[t, counts.get(t, 0)] for t in ticks]})
    else:
        series.append({"label": "logs", "values": [[t, total.get(t, 0)] for t in ticks]})
    return {"series": series, "total": len(res["entries"]), "step": step}


def log_stats(q: str, start: int, end: int) -> dict:
    res = query_logs(q, start, end, limit=200000)
    by_level: dict[str, int] = {}
    by_service: dict[str, int] = {}
    for e in res["entries"]:
        by_level[e["level"]] = by_level.get(e["level"], 0) + 1
        by_service[e["service"] or "(none)"] = by_service.get(e["service"] or "(none)", 0) + 1
    return {
        "total": res["count"],
        "by_level": dict(sorted(by_level.items(), key=lambda kv: -kv[1])),
        "by_service": dict(sorted(by_service.items(), key=lambda kv: -kv[1])[:15]),
    }


# --------------------------------------------------------------------------
# Traces
# --------------------------------------------------------------------------
def search_traces(
    start: int,
    end: int,
    service: str = "",
    operation: str = "",
    status: str = "",
    min_duration_ms: float = 0,
    max_duration_ms: float = 0,
    tag: str = "",
    limit: int = 50,
    sort: str = "duration",
) -> dict:
    conn = db.connect()
    where = ["start_us>=?", "start_us<=?", "parent_id IS NULL"]
    args: list = [start * 1_000_000, end * 1_000_000]
    if service:
        where.append("service=?")
        args.append(service)
    if operation:
        where.append("name LIKE ?")
        args.append(f"%{operation}%")
    if status:
        where.append("status=?")
        args.append(status)
    if min_duration_ms:
        where.append("duration_us>=?")
        args.append(min_duration_ms * 1000)
    if max_duration_ms:
        where.append("duration_us<=?")
        args.append(max_duration_ms * 1000)
    if tag:
        where.append("attrs LIKE ?")
        args.append(f"%{tag}%")
    order = "duration_us DESC" if sort == "duration" else "start_us DESC"
    rows = conn.execute(f"SELECT * FROM spans WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?", (*args, limit)).fetchall()
    traces = []
    for r in rows:
        span_count, err_count = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM spans WHERE trace_id=?", (r["trace_id"],)
        ).fetchone()
        traces.append(
            {
                "trace_id": r["trace_id"],
                "root": r["name"],
                "service": r["service"],
                "start_us": r["start_us"],
                "duration_ms": round(r["duration_us"] / 1000, 2),
                "status": r["status"],
                "spans": span_count,
                "errors": err_count or 0,
                "attrs": db.jloads(r["attrs"], {}),
            }
        )
    return {"traces": traces, "count": len(traces)}


def get_trace(trace_id: str) -> dict | None:
    conn = db.connect()
    rows = conn.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY start_us", (trace_id,)).fetchall()
    if not rows:
        return None
    spans = []
    t0 = rows[0]["start_us"]
    for r in rows:
        spans.append(
            {
                "span_id": r["span_id"],
                "parent_id": r["parent_id"],
                "service": r["service"],
                "name": r["name"],
                "start_ms": round((r["start_us"] - t0) / 1000, 2),
                "duration_ms": round(r["duration_us"] / 1000, 3),
                "status": r["status"],
                "kind": r["kind"],
                "attrs": db.jloads(r["attrs"], {}),
                "events": db.jloads(r["events"], []),
            }
        )
    root = rows[0]
    services = sorted({r["service"] for r in rows})
    return {
        "trace_id": trace_id,
        "start_us": t0,
        "duration_ms": round(max(r["duration_us"] for r in rows) / 1000, 2),
        "services": services,
        "root": {"name": root["name"], "service": root["service"], "status": root["status"]},
        "error": any(r["status"] == "error" for r in rows),
        "spans": spans,
    }


def trace_aggregates(start: int, end: int, group_by: str = "service") -> dict:
    conn = db.connect()
    field = "service" if group_by == "service" else "name"
    rows = conn.execute(
        f"""SELECT {field} AS k, COUNT(*) n,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errs,
                   AVG(duration_us) avg_us,
                   MAX(duration_us) max_us
            FROM spans WHERE start_us>=? AND start_us<=? AND parent_id IS NULL
            GROUP BY {field} ORDER BY n DESC LIMIT 250""",
        (start * 1_000_000, end * 1_000_000),
    ).fetchall()
    out = []
    for r in rows:
        durations = [
            row[0]
            for row in conn.execute(
                f"SELECT duration_us FROM spans WHERE {field}=? AND start_us>=? AND start_us<=? AND parent_id IS NULL ORDER BY duration_us",
                (r["k"], start * 1_000_000, end * 1_000_000),
            )
        ]
        n = len(durations)
        p = lambda q: durations[min(n - 1, int(q * n))] / 1000 if n else 0
        out.append(
            {
                "key": r["k"] or "(none)",
                "count": r["n"],
                "errors": r["errs"] or 0,
                "error_rate": round((r["errs"] or 0) / r["n"] * 100, 2) if r["n"] else 0,
                "avg_ms": round((r["avg_us"] or 0) / 1000, 2),
                "p50_ms": round(p(0.5), 2),
                "p95_ms": round(p(0.95), 2),
                "p99_ms": round(p(0.99), 2),
                "max_ms": round((r["max_us"] or 0) / 1000, 2),
            }
        )
    return {"groups": out, "group_by": group_by}


def trace_volume(start: int, end: int, step: int) -> dict:
    conn = db.connect()
    step = max(step, 1)
    rows = conn.execute(
        f"""SELECT (start_us/1000000/{step})*{step} AS b, service, COUNT(*) n,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) errs
            FROM spans WHERE start_us>=? AND start_us<=? AND parent_id IS NULL
            GROUP BY b, service ORDER BY b""",
        (start * 1_000_000, end * 1_000_000),
    ).fetchall()
    svc: dict[str, dict[int, int]] = {}
    err: dict[int, int] = {}
    for r in rows:
        svc.setdefault(r["service"], {})[r["b"]] = r["n"]
        err[r["b"]] = err.get(r["b"], 0) + (r["errs"] or 0)
    ticks = list(range(start - start % step, end + 1, step))
    series = [{"label": s, "values": [[t, counts.get(t, 0)] for t in ticks]} for s, counts in svc.items()]
    errors = [{"label": "errors", "values": [[t, err.get(t, 0)] for t in ticks]}]
    return {"series": series, "errors": errors, "step": step}


def service_graph(start: int, end: int) -> dict:
    """Derive service-to-service edges from parent/child span relationships."""
    conn = db.connect()
    rows = conn.execute(
        """SELECT p.service AS parent, c.service AS child, COUNT(*) n,
                  AVG(c.duration_us) avg_us,
                  SUM(CASE WHEN c.status='error' THEN 1 ELSE 0 END) errs
           FROM spans c JOIN spans p ON p.trace_id=c.trace_id AND p.span_id=c.parent_id
           WHERE c.start_us>=? AND c.start_us<=?
           GROUP BY parent, child""",
        (start * 1_000_000, end * 1_000_000),
    ).fetchall()
    nodes = {}
    edges = []
    for r in rows:
        if r["parent"] == r["child"]:
            continue
        edges.append(
            {"source": r["parent"], "target": r["child"], "calls": r["n"], "avg_ms": round((r["avg_us"] or 0) / 1000, 2), "errors": r["errs"] or 0}
        )
        for s in (r["parent"], r["child"]):
            nodes.setdefault(s, {"name": s})
    durations: dict[str, list[int]] = {}
    errors: dict[str, int] = {}
    for r in conn.execute(
        "SELECT service, duration_us, status FROM spans WHERE start_us>=? AND start_us<=?",
        (start * 1_000_000, end * 1_000_000),
    ):
        durations.setdefault(r["service"], []).append(r["duration_us"] or 0)
        if r["status"] == "error":
            errors[r["service"]] = errors.get(r["service"], 0) + 1
    for name, values in durations.items():
        values.sort()
        node = nodes.setdefault(name, {"name": name})
        node.update(
            {
                "spans": len(values),
                "errors": errors.get(name, 0),
                "p95_ms": round(values[min(len(values) - 1, int(0.95 * len(values)))] / 1000, 2),
            }
        )
    return {"nodes": list(nodes.values()), "edges": edges}


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------
def search(term: str, start: int, end: int, limit: int = 8) -> dict:
    conn = db.connect()
    like = f"%{term}%"
    logs = conn.execute(
        "SELECT ts, service, level, body FROM logs WHERE ts BETWEEN ? AND ? AND body LIKE ? ORDER BY ts DESC LIMIT ?",
        (start, end, like, limit),
    ).fetchall()
    traces = conn.execute(
        "SELECT DISTINCT trace_id, name, service, start_us, duration_us, status FROM spans WHERE start_us BETWEEN ? AND ? AND (name LIKE ? OR service LIKE ?) ORDER BY start_us DESC LIMIT ?",
        (start * 1_000_000, end * 1_000_000, like, like, limit),
    ).fetchall()
    issues = conn.execute(
        "SELECT id, title, culprit, count, last_seen, status FROM issues WHERE title LIKE ? OR culprit LIKE ? ORDER BY last_seen DESC LIMIT ?",
        (like, like, limit),
    ).fetchall()
    alerts = conn.execute(
        "SELECT id, name, severity, state, summary FROM alert_instances WHERE name LIKE ? OR summary LIKE ? LIMIT ?",
        (like, like, limit),
    ).fetchall()
    dashboards = conn.execute("SELECT uid, title FROM dashboards WHERE title LIKE ? LIMIT ?", (like, limit)).fetchall()
    return {
        "term": term,
        "logs": [dict(r) for r in logs],
        "traces": [dict(r) for r in traces],
        "issues": [dict(r) for r in issues],
        "alerts": [dict(r) for r in alerts],
        "dashboards": [dict(r) for r in dashboards],
    }
