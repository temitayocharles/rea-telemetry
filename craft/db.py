"""Storage for TelemetryCraft.

One SQLite file holds every signal: metrics, logs, traces, errors, alerts,
incidents, synthetics and configuration. No server to run, no cluster to babysit.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- Time series -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS series (
    id INTEGER PRIMARY KEY,
    metric TEXT NOT NULL,
    labels TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'gauge',
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS series_metric ON series(metric);

CREATE TABLE IF NOT EXISTS samples (
    series_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);

-- Logs --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    service TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',
    body TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '{}',
    attrs TEXT NOT NULL DEFAULT '{}',
    trace_id TEXT,
    span_id TEXT
);
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS logs_service ON logs(service, ts);

-- Traces ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_id TEXT,
    service TEXT NOT NULL,
    name TEXT NOT NULL,
    start_us INTEGER NOT NULL,
    duration_us INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    kind TEXT NOT NULL DEFAULT 'server',
    attrs TEXT NOT NULL DEFAULT '{}',
    events TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (trace_id, span_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS spans_start ON spans(start_us);
CREATE INDEX IF NOT EXISTS spans_service ON spans(service, start_us);

-- Error tracking ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    culprit TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'error',
    service TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    users INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unresolved',
    assigned TEXT NOT NULL DEFAULT '',
    release TEXT NOT NULL DEFAULT '',
    stack TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    trace_id TEXT,
    user_id TEXT,
    release TEXT,
    context TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_issue ON events(issue_id, ts);

-- Alerting ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    expr TEXT NOT NULL,
    for_s INTEGER NOT NULL DEFAULT 300,
    severity TEXT NOT NULL DEFAULT 'warning',
    summary TEXT NOT NULL DEFAULT '',
    runbook TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT 'default',
    enabled INTEGER NOT NULL DEFAULT 1,
    labels TEXT NOT NULL DEFAULT '{}',
    format TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alert_instances (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    rule_id INTEGER,
    name TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    summary TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '{}',
    value REAL,
    expr TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    active_since INTEGER NOT NULL,
    last_eval INTEGER NOT NULL,
    ends_at INTEGER,
    acked_by TEXT,
    acked_at INTEGER,
    resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS silences (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    matchers TEXT NOT NULL,
    starts_at INTEGER NOT NULL,
    ends_at INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    receiver TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    alert_name TEXT,
    status TEXT NOT NULL DEFAULT 'delivered'
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    rotation TEXT NOT NULL,
    members TEXT NOT NULL,
    handoff_hour INTEGER NOT NULL DEFAULT 9
);

CREATE TABLE IF NOT EXISTS services (
    name TEXT PRIMARY KEY,
    display TEXT NOT NULL,
    team TEXT NOT NULL DEFAULT '',
    tier INTEGER NOT NULL DEFAULT 2,
    repo TEXT NOT NULL DEFAULT '',
    oncall TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT ''
);

-- SLOs --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slos (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    service TEXT NOT NULL,
    objective REAL NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 30,
    sli_expr TEXT NOT NULL,
    total_expr TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- Synthetics & status page ------------------------------------------------
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'http',
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'GET',
    expect_status INTEGER NOT NULL DEFAULT 200,
    expect_body TEXT NOT NULL DEFAULT '',
    interval_s INTEGER NOT NULL DEFAULT 60,
    timeout_ms INTEGER NOT NULL DEFAULT 5000,
    regions TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    group_name TEXT NOT NULL DEFAULT 'Core',
    public INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS check_results (
    id INTEGER PRIMARY KEY,
    check_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    status_code INTEGER,
    latency_ms REAL,
    region TEXT NOT NULL DEFAULT 'iad',
    error TEXT
);
CREATE INDEX IF NOT EXISTS check_results_ck ON check_results(check_id, ts);

-- Incidents ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'investigating',
    impact TEXT NOT NULL DEFAULT 'minor',
    commander TEXT NOT NULL DEFAULT '',
    responders TEXT NOT NULL DEFAULT '[]',
    services TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL,
    detected_at INTEGER NOT NULL,
    resolved_at INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    postmortem TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS incident_events (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS incident_events_i ON incident_events(incident_id, ts);

-- Saved UI objects --------------------------------------------------------
CREATE TABLE IF NOT EXISTS dashboards (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT 'General',
    tags TEXT NOT NULL DEFAULT '[]',
    panels TEXT NOT NULL DEFAULT '[]',
    templating TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL,
    starred INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY,
    app TEXT NOT NULL,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS loadtests (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    script TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    started_at INTEGER,
    finished_at INTEGER,
    summary TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS loadtest_points (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    vus INTEGER NOT NULL,
    rps REAL NOT NULL,
    p50 REAL NOT NULL,
    p95 REAL NOT NULL,
    p99 REAL NOT NULL,
    errors REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS loadtest_points_t ON loadtest_points(test_id, ts);

-- Collector pipeline config ----------------------------------------------
CREATE TABLE IF NOT EXISTS pipelines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    signal TEXT NOT NULL,
    receivers TEXT NOT NULL DEFAULT '[]',
    processors TEXT NOT NULL DEFAULT '[]',
    exporters TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

_LOCAL = threading.local()
_LOCK = threading.Lock()
_initialised = set()

# Columns added after the first release. Applied to existing databases on open
# (SQLite has no "ADD COLUMN IF NOT EXISTS"), so upgrading never loses data.
MIGRATIONS = (
    ("rules", "format", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def db_path() -> str:
    return os.environ.get(
        "CRAFT_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "telemetry.db")
    )


def _regexp(pattern: str, value: str | None) -> int:
    if value is None:
        return 0
    try:
        return 1 if re.fullmatch(pattern, str(value)) else 0
    except re.error:
        return 0


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = (path, os.getpid(), threading.get_ident())
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "key", None) == key:
        return conn
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA cache_size=-32000")
    conn.create_function("REGEXP", 2, _regexp)
    _LOCAL.conn = conn
    _LOCAL.key = key
    with _LOCK:
        if path not in _initialised:
            conn.executescript(SCHEMA)
            _migrate(conn)
            _initialised.add(path)
    return conn


def reset(path: str | None = None) -> None:
    path = path or db_path()
    if os.path.exists(path):
        os.remove(path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    with _LOCK:
        _initialised.discard(path)
    _LOCAL.conn = None


def get_meta(k: str, default=None):
    row = connect().execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_meta(k: str, v) -> None:
    connect().execute("INSERT INTO meta(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def now() -> int:
    """Unix seconds. Seed/demo data is anchored to this clock."""
    return int(time.time())


def jloads(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
