"""Ingest: turn wire formats into rows.

Supported on the way in:
  metrics  Prometheus remote-write JSON, OTLP/HTTP JSON metrics, the simple
           JSON/line protocol below, and scrape targets (Prometheus text format).
  logs     Loki push JSON, OTLP JSON logs, JSON lines.
  traces   OTLP JSON traces, Jaeger-style JSON.
  errors   Sentry-style store payloads (simplified envelope).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from . import db

COUNTER_HINTS = ("_total", "_count", "_sum", "_bucket", "_seconds_total", "_bytes_total")


def _fingerprint(metric: str, labels: dict) -> str:
    payload = metric + "\x00" + json.dumps(labels, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


def infer_type(metric: str, explicit: str | None = None) -> str:
    if explicit in ("gauge", "counter", "histogram", "summary"):
        return explicit
    return "counter" if metric.endswith(COUNTER_HINTS) else "gauge"


class MetricWriter:
    """Batches series resolution and sample inserts in one transaction."""

    def __init__(self, conn=None):
        self.conn = conn or db.connect()
        self.cache: dict[str, int] = {}
        self.batch: list[tuple[int, int, float]] = []
        self.new_series = False

    def series_id(self, metric: str, labels: dict, stype: str | None = None) -> int:
        fp = _fingerprint(metric, labels)
        sid = self.cache.get(fp)
        if sid is not None:
            return sid
        row = self.conn.execute("SELECT id FROM series WHERE fingerprint=?", (fp,)).fetchone()
        if row:
            sid = row["id"]
        else:
            now = db.now()
            cur = self.conn.execute(
                "INSERT INTO series(metric, labels, fingerprint, type, first_ts, last_ts) VALUES(?,?,?,?,?,?)",
                (metric, json.dumps(labels, sort_keys=True), fp, infer_type(metric, stype), 99999999999, 0),
            )
            sid = cur.lastrowid
            self.new_series = True
        self.cache[fp] = sid
        return sid

    def begin(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")

    def add(self, metric: str, labels: dict, ts: float, value: float, stype=None):
        if value != value:  # NaN
            return
        self.begin()
        sid = self.series_id(metric, labels, stype)
        seconds = float(ts) / 1000.0 if float(ts) > 1e11 else float(ts)
        self.batch.append((sid, int(seconds), float(value)))

    def flush(self) -> int:
        n = 0
        if self.batch:
            self.conn.executemany("INSERT OR REPLACE INTO samples(series_id, ts, value) VALUES(?,?,?)", self.batch)
            n = len(self.batch)
            ids = sorted({b[0] for b in self.batch})
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                self.conn.execute(
                    f"""UPDATE series SET
                        first_ts=MIN(first_ts,(SELECT MIN(ts) FROM samples WHERE series_id=series.id)),
                        last_ts=MAX(last_ts,(SELECT MAX(ts) FROM samples WHERE series_id=series.id))
                        WHERE id IN ({','.join('?' * len(chunk))})""",
                    chunk,
                )
            self.batch = []
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")
        if self.new_series:
            from .promql import bump_series_version

            bump_series_version()
            self.new_series = False
        return n

    def rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            self.flush()
        else:
            self.rollback()


# --------------------------------------------------------------------------
# Prometheus formats
# --------------------------------------------------------------------------
def parse_prom_text(text: str, job="", instance="") -> list[tuple[str, dict, int, float]]:
    out = []
    ts_now = int(time.time() * 1000)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if " " not in line:
                continue
            head, rest = line.rsplit(" ", 1)
            parts = head.split(" ")
            # handle timestamp column
            stamp = ts_now
            if len(parts) > 1 and parts[-1].isdigit():
                stamp = int(parts[-1])
                head = " ".join(parts[:-1])
            name_part, labels = _split_metric(head)
            labels.setdefault("job", job)
            labels.setdefault("instance", instance)
            out.append((name_part, labels, stamp, float(rest)))
        except (ValueError, IndexError):
            continue
    return out


def _split_metric(head: str):
    if "{" in head:
        name = head.split("{")[0]
        inner = head[head.index("{") + 1 : head.rindex("}")]
        labels = {}
        for pair in _split_labels(inner):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            labels[k.strip()] = v.strip().strip('"').replace('\\"', '"').replace("\\n", "\n")
        return name, labels
    return head, {}


def _split_labels(inner: str) -> list[str]:
    out, buf, in_quote, esc = [], [], False, False
    for ch in inner:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            buf.append(ch)
            esc = True
        elif ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == "," and not in_quote:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [p for p in out if p.strip()]


def ingest_remote_write(payload: dict) -> int:
    with MetricWriter() as w:
        for ts in payload.get("timeseries", []):
            metric = {k: v for k, v in ts.get("metric", {}).items()}
            name = metric.pop("__name__", "")
            if not name:
                continue
            for pair in ts.get("values", []):
                try:
                    w.add(name, metric, float(pair[0]), float(pair[1]))
                except (TypeError, ValueError, IndexError):
                    continue
        return len(w.batch)


def _otlp_attrs(attrs) -> dict:
    out = {}
    for a in attrs or []:
        key = a.get("key")
        val = a.get("value", {})
        if key is None:
            continue
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in val:
                out[key] = val[kind]
                break
        else:
            if "arrayValue" in val:
                out[key] = [list(v.values())[0] for v in val["arrayValue"].get("values", [])]
    return out


def ingest_otlp_metrics(payload: dict) -> int:
    count = 0
    with MetricWriter() as w:
        for rm in payload.get("resourceMetrics", []):
            base = _otlp_attrs(rm.get("resource", {}).get("attributes"))
            for sm in rm.get("scopeMetrics", []):
                scope = sm.get("scope", {}).get("name", "")
                if scope:
                    base.setdefault("otel_scope", scope)
                for m in sm.get("metrics", []):
                    name = m.get("name", "")
                    if not name:
                        continue
                    for kind, stype in (("gauge", "gauge"), ("sum", "counter"), ("histogram", "histogram")):
                        body = m.get(kind)
                        if not body:
                            continue
                        for dp in body.get("dataPoints", []):
                            labels = {**base, **_otlp_attrs(dp.get("attributes"))}
                            ts = dp.get("timeUnixNano")
                            ts_ms = int(ts) / 1e6 if ts else time.time() * 1000
                            if "asDouble" in dp or "asInt" in dp:
                                val = float(dp.get("asDouble", dp.get("asInt", 0)))
                                w.add(name, labels, ts_ms, val, stype)
                                count += 1
                            elif "sum" in dp:
                                w.add(name + "_sum", labels, ts_ms, float(dp["sum"]), stype)
                                w.add(name + "_count", labels, ts_ms, float(dp.get("count", 0)), stype)
                                count += 2
                            if "bucketCounts" in dp and "sum" not in dp:
                                bounds = dp.get("explicitBounds", [])
                                for i, bc in enumerate(dp["bucketCounts"]):
                                    le = bounds[i] if i < len(bounds) else float("inf")
                                    lab = dict(labels)
                                    lab["le"] = "+Inf" if le == float("inf") else str(le)
                                    w.add(name + "_bucket", lab, ts_ms, float(bc), "histogram")
                                    count += 1
        return count


def ingest_otlp_logs(payload: dict) -> int:
    conn = db.connect()
    rows = []
    for rl in payload.get("resourceLogs", []):
        base = _otlp_attrs(rl.get("resource", {}).get("attributes"))
        service = base.get("service.name", "")
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                attrs = {**base, **_otlp_attrs(rec.get("attributes"))}
                body = rec.get("body", {})
                text = body.get("stringValue") or json.dumps(body)
                ts = int(rec.get("timeUnixNano", 0)) // 1_000_000_000 or db.now()
                severity = str(rec.get("severityText", "info")).lower()
                rows.append((ts, service, severity, text, json.dumps({}), json.dumps(attrs), rec.get("traceId"), rec.get("spanId")))
    if rows:
        conn.executemany(
            "INSERT INTO logs(ts, service, level, body, labels, attrs, trace_id, span_id) VALUES(?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def ingest_loki(payload: dict) -> int:
    conn = db.connect()
    rows = []
    for stream in payload.get("streams", []):
        labels = stream.get("stream", {})
        service = labels.get("service_name") or labels.get("service") or labels.get("app") or ""
        for pair in stream.get("values", []):
            try:
                ns, line = int(pair[0]), pair[1]
            except (TypeError, ValueError, IndexError):
                continue
            level = ""
            body = line
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    body = obj.get("msg") or obj.get("message") or line
                    level = str(obj.get("level", "")).lower()
                except ValueError:
                    pass
            rows.append((ns // 1_000_000_000, service, level or labels.get("level", "info"), body, json.dumps(labels), "{}", None, None))
    if rows:
        conn.executemany(
            "INSERT INTO logs(ts, service, level, body, labels, attrs, trace_id, span_id) VALUES(?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def ingest_json_logs(records: list[dict]) -> int:
    conn = db.connect()
    rows = []
    for r in records:
        ts = r.get("ts") or r.get("timestamp")
        if isinstance(ts, str):
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = db.now()
        ts = int(ts or db.now())
        if ts > 10_000_000_000:
            ts //= 1000
        rows.append(
            (
                ts,
                r.get("service", ""),
                str(r.get("level", "info")).lower(),
                str(r.get("body") or r.get("message") or ""),
                json.dumps(r.get("labels", {})),
                json.dumps({k: v for k, v in r.items() if k not in ("ts", "timestamp", "service", "level", "body", "message", "labels")}),
                r.get("trace_id"),
                r.get("span_id"),
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO logs(ts, service, level, body, labels, attrs, trace_id, span_id) VALUES(?,?,?,?,?,?,?,?)", rows
        )
    return len(rows)


def ingest_otlp_traces(payload: dict) -> int:
    conn = db.connect()
    rows = []
    for rs in payload.get("resourceSpans", []):
        base = _otlp_attrs(rs.get("resource", {}).get("attributes"))
        service = base.get("service.name", "unknown")
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                attrs = _otlp_attrs(span.get("attributes"))
                try:
                    start = int(span.get("startTimeUnixNano", 0)) // 1000
                    end = int(span.get("endTimeUnixNano", 0)) // 1000
                except (TypeError, ValueError):
                    continue
                status = "ok"
                code = str(span.get("status", {}).get("code", "")).upper()
                if code in ("STATUS_CODE_ERROR", "2", "ERROR"):
                    status = "error"
                events = json.dumps(
                    [{"name": e.get("name"), "ts": int(e.get("timeUnixNano", 0)) // 1_000_000, "attrs": _otlp_attrs(e.get("attributes"))} for e in span.get("events", [])]
                )
                rows.append(
                    (
                        span.get("traceId", ""),
                        span.get("spanId", ""),
                        span.get("parentSpanId") or None,
                        service,
                        span.get("name", ""),
                        start,
                        max(end - start, 0),
                        status,
                        str(span.get("kind", "server")).lower().replace("span_kind_", ""),
                        json.dumps(attrs),
                        events,
                    )
                )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO spans(trace_id, span_id, parent_id, service, name, start_us, duration_us, status, kind, attrs, events) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def ingest_json_traces(records: list[dict]) -> int:
    conn = db.connect()
    rows = []
    for r in records:
        start_us = int(r.get("start_us") or int(float(r.get("start", time.time())) * 1e6))
        dur_us = int(r.get("duration_us") or int(float(r.get("duration_ms", 0)) * 1000))
        rows.append(
            (
                r.get("trace_id", ""),
                r.get("span_id", ""),
                r.get("parent_id"),
                r.get("service", ""),
                r.get("name", ""),
                start_us,
                dur_us,
                r.get("status", "ok"),
                r.get("kind", "server"),
                json.dumps(r.get("attrs", {})),
                json.dumps(r.get("events", [])),
            )
        )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO spans(trace_id, span_id, parent_id, service, name, start_us, duration_us, status, kind, attrs, events) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


# --------------------------------------------------------------------------
# Error tracking
# --------------------------------------------------------------------------
def ingest_event(payload: dict) -> int:
    conn = db.connect()
    exc = payload.get("exception", {}) or {}
    stack = payload.get("stack") or exc.get("stack") or []
    title = payload.get("title") or exc.get("type") or "Error"
    msg = payload.get("message") or exc.get("value") or ""
    if msg and msg not in title:
        title = f"{title}: {msg}" if title == (exc.get("type") or "") else title
    culprit = payload.get("culprit") or (payload.get("service", "") and f"{payload.get('service')}")
    fingerprint = payload.get("fingerprint") or _event_fingerprint(title, culprit, stack)
    now = int(payload.get("ts") or db.now())
    service = payload.get("service", "")
    level = payload.get("level", "error")
    user = payload.get("user_id") or (payload.get("user") or {}).get("id") or ""
    row = conn.execute("SELECT * FROM issues WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row:
        issue_id = row["id"]
        conn.execute(
            "UPDATE issues SET last_seen=?, count=count+1, users=users+?, status=CASE WHEN status='resolved' THEN 'unresolved' ELSE status END WHERE id=?",
            (now, 1 if user else 0, issue_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO issues(fingerprint, title, culprit, level, service, first_seen, last_seen, count, users, stack, tags, release) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fingerprint,
                title,
                culprit,
                level,
                service,
                now,
                now,
                1,
                1 if user else 0,
                json.dumps(stack),
                json.dumps(payload.get("tags", {})),
                payload.get("release", ""),
            ),
        )
        issue_id = cur.lastrowid
    conn.execute(
        "INSERT INTO events(issue_id, ts, trace_id, user_id, release, context) VALUES(?,?,?,?,?,?)",
        (issue_id, now, payload.get("trace_id"), user, payload.get("release", ""), json.dumps(payload.get("context", payload.get("tags", {})))),
    )
    return issue_id


def _event_fingerprint(title: str, culprit: str, stack) -> str:
    frames = stack if isinstance(stack, list) else []
    head = ""
    if frames:
        f = frames[0] if isinstance(frames[0], dict) else {}
        head = f"{f.get('module', '')}:{f.get('function', '')}:{f.get('line', '')}"
    return hashlib.blake2b(f"{title}\x00{culprit}\x00{head}".encode(), digest_size=16).hexdigest()


# --------------------------------------------------------------------------
# Scraping (pull model, like a Prometheus server does)
# --------------------------------------------------------------------------
def scrape_once(target: dict) -> dict:
    url = target["url"]
    timeout = target.get("timeout_ms", 5000) / 1000.0
    started = time.time()
    result = {"url": url, "ok": False, "samples": 0, "error": None}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TelemetryCraft/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        samples = parse_prom_text(body, job=target.get("job", ""), instance=target.get("instance", url))
        with MetricWriter() as w:
            for metric, labels, ts, value in samples:
                w.add(metric, labels, ts, value)
        result.update(ok=True, samples=len(samples))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["error"] = str(exc)
    result["duration_ms"] = round((time.time() - started) * 1000, 2)
    return result


# --------------------------------------------------------------------------
# Retention & diagnostics
# --------------------------------------------------------------------------
def prune(retention_days: int | None = None) -> dict:
    retention_days = retention_days or int(os.environ.get("CRAFT_RETENTION_DAYS", 30))
    conn = db.connect()
    cutoff = db.now() - retention_days * 86400
    cuts = {}
    cuts["samples"] = conn.execute("DELETE FROM samples WHERE ts<?", (cutoff,)).rowcount
    cuts["logs"] = conn.execute("DELETE FROM logs WHERE ts<?", (cutoff,)).rowcount
    cuts["spans"] = conn.execute("DELETE FROM spans WHERE start_us<?", (cutoff * 1_000_000,)).rowcount
    cuts["check_results"] = conn.execute("DELETE FROM check_results WHERE ts<?", (cutoff,)).rowcount
    conn.execute("DELETE FROM series WHERE last_ts<?", (cutoff,))
    return cuts


def stats() -> dict:
    conn = db.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]
    size = 0
    path = db.db_path()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            size += os.path.getsize(path + suffix)
    hour_ago = db.now() - 3600
    return {
        "series": q("SELECT COUNT(*) FROM series"),
        "samples": q("SELECT COUNT(*) FROM samples"),
        "samples_1h": q(f"SELECT COUNT(*) FROM samples WHERE ts>{hour_ago}"),
        "logs": q("SELECT COUNT(*) FROM logs"),
        "logs_1h": q(f"SELECT COUNT(*) FROM logs WHERE ts>{hour_ago}"),
        "spans": q("SELECT COUNT(*) FROM spans"),
        "span_errors": q("SELECT COUNT(*) FROM spans WHERE status='error'"),
        "issues": q("SELECT COUNT(*) FROM issues"),
        "alerts_firing": q("SELECT COUNT(*) FROM alert_instances WHERE state='firing'"),
        "alerts_pending": q("SELECT COUNT(*) FROM alert_instances WHERE state='pending'"),
        "db_bytes": size,
        "db_path": path,
        "retention_days": int(os.environ.get("CRAFT_RETENTION_DAYS", 30)),
        "engine": "sqlite",
    }
