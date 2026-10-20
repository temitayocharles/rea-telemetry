"""Command line interface — everything the UI does, without the UI."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import alerting, db, incidents, loadtest, promql, query, seed, slo, store, synthetics

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
COLORS = {"ok": "\033[32m", "warn": "\033[33m", "crit": "\033[31m", "info": "\033[36m", "accent": "\033[38;5;150m"}


def c(text, tone=""):
    if not sys.stdout.isatty():
        return str(text)
    return f"{COLORS.get(tone, '')}{text}{RESET}"


def out(value, as_json=False):
    if as_json:
        print(json.dumps(value, indent=2, default=str))
    else:
        print(value)


def table(headers, rows, aligns=None):
    aligns = aligns or [""] * len(headers)
    widths = [len(h) for h in headers]
    cells = []
    for row in rows:
        rendered = [str(v) for v in row]
        cells.append(rendered)
        widths = [max(w, len(v)) for w, v in zip(widths, rendered)]
    print(DIM + "  ".join(h.ljust(w) if a != "right" else h.rjust(w) for h, w, a in zip(headers, widths, aligns)) + RESET)
    for row in cells:
        print("  ".join(v.ljust(w) if a != "right" else v.rjust(w) for v, w, a in zip(row, widths, aligns)))


def fmtnum(value) -> str:
    """Compact, readable numbers: 459.99, 12.3k, -8.38B."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v:
        return "NaN"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}k"
    if a >= 1:
        return f"{v:.4g}"
    if a == 0:
        return "0"
    return f"{v:.3g}"


def since(text: str) -> int:
    try:
        ms = promql.parse_duration(text)
    except promql.ParseError:
        raise SystemExit(f"bad duration: {text}")
    return db.now() - ms // 1000


# --------------------------------------------------------------------------
def cmd_serve(args):
    from . import auth as authmod
    from . import server

    if args.auth_token or args.auth_user or args.no_auth:
        server.AUTH = authmod.Auth(
            token=args.auth_token or "",
            user=args.auth_user or "",
            password=args.auth_password or "",
        )
    server.serve(
        args.port, args.host, autoseed=not args.no_seed,
        tls={"cert": args.tls_cert or "", "key": args.tls_key or "", "client_ca": args.tls_client_ca or ""},
    )


def cmd_seed(args):
    print(json.dumps(seed.seed(args.self_url, force=args.force), indent=2))


def cmd_status(args):
    st = store.stats()
    alerts = alerting.list_alerts()
    firing = [a for a in alerts if a["state"] == "firing"]
    open_inc = incidents.open_incidents()
    print(f"{BOLD}TelemetryCraft{RESET}  {DIM}{st['db_path']}{RESET}")
    print(f"  series        {st['series']:,}")
    print(f"  samples       {st['samples']:,}  ({st['samples_1h']:,} in the last hour)")
    print(f"  logs          {st['logs']:,}")
    print(f"  spans         {st['spans']:,}")
    print(f"  alerts        {c(str(len(firing)) + ' firing', 'crit' if firing else 'ok')}")
    print(f"  incidents     {len(open_inc)} open")
    print(f"  disk          {st['db_bytes'] / 1e6:.1f} MB (retention {st['retention_days']}d)")


def cmd_query(args):
    if args.range:
        end = db.now() * 1000
        start = end - promql.parse_duration(args.range)
        data = promql.query_range(args.expr, start, end, args.step)
        if args.json:
            return out({"status": "success", "data": data}, True)
        rows = []
        for s in data["result"]:
            values = [v for _, v in s["values"]]
            rows.append([", ".join(f"{k}={v}" for k, v in s["metric"].items() if k != "__name__") or s["metric"].get("__name__", "{}"),
                         fmtnum(values[-1]) if values else "—", len(values)])
        table(["series", "last", "points"], rows, ["", "right", "right"])
        return
    data = promql.query_instant(args.expr, db.now() * 1000)
    if args.json:
        return out({"status": "success", "data": data}, True)
    if data["resultType"] == "scalar":
        return print(data["result"][1])
    if not data["result"]:
        return print(c("no series matched", "warn"))
    rows = [[" ".join(f"{k}={v}" for k, v in s["metric"].items() if k != "__name__") or s["metric"].get("__name__", "{}"), s["value"][1]] for s in data["result"]]
    table(["series", "value"], rows, ["", "right"])


def cmd_metrics(args):
    catalog = []
    conn = db.connect()
    for row in conn.execute("SELECT metric, COUNT(*) n, type FROM series GROUP BY metric ORDER BY metric"):
        catalog.append([row["metric"], row["n"], row["type"]])
    table(["metric", "series", "type"], catalog, ["", "right", ""])


def cmd_labels(args):
    if args.name:
        return print("\n".join(promql.label_values(args.name)))
    print("\n".join(promql.label_names()))


def cmd_logs(args):
    start = since(args.since)
    res = query.query_logs(args.query, start, db.now(), args.limit)
    if args.json:
        return out(res, True)
    for e in res["entries"]:
        ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        tone = "crit" if e["level"] == "error" else "warn" if e["level"] == "warn" else ""
        print(f"{DIM}{ts}{RESET} {c(e['level'][:5].ljust(5), tone)} {c((e['service'] or '-')[:22].ljust(22), 'info')} {e['body']}")
    print(DIM + f"\n{res['count']} lines" + RESET)


def cmd_traces(args):
    start = since(args.since)
    if args.trace_id:
        trace = query.get_trace(args.trace_id)
        if not trace:
            raise SystemExit("trace not found")
        if args.json:
            return out(trace, True)
        print(f"{BOLD}{trace['root']['name']}{RESET}  {trace['trace_id']}  {trace['duration_ms']}ms  {' '.join(trace['services'])}")
        for span in trace["spans"]:
            indent = "  " * (0 if not span["parent_id"] else 1)
            tone = "crit" if span["status"] == "error" else ""
            print(f"{indent}{c(span['service'][:18].ljust(18), 'info')} {span['name'][:52].ljust(52)} {span['duration_ms']:>9.2f}ms {c(span['status'], tone)}")
        return
    res = query.search_traces(start, db.now(), service=args.service, status=args.status, min_duration_ms=args.min_duration, limit=args.limit)
    if args.json:
        return out(res, True)
    rows = [[t["trace_id"][:16], t["service"], t["root"][:38], f"{t['duration_ms']:.0f}ms", t["spans"], t["status"]] for t in res["traces"]]
    table(["trace", "service", "root", "duration", "spans", "status"], rows, ["", "", "", "right", "right", ""])


def cmd_alerts(args):
    if args.evaluate:
        print(json.dumps(alerting.evaluate(), indent=2))
        return
    if args.ack:
        alerting.ack(int(args.ack), args.by)
        return print(f"acknowledged {args.ack} by {args.by}")
    if args.json:
        return out({"alerts": alerting.list_alerts(), "last_eval": alerting.last_evaluation()}, True)
    rows = []
    for a in alerting.list_alerts():
        tone = "crit" if a["state"] == "firing" else "warn" if a["state"] == "pending" else "ok"
        shown = a.get("value_display") or fmtnum(a["value"])
        if a.get("value_note"):
            shown = f"{shown} ({a['value_note'].split(' — ')[0]})"
        rows.append([c(a["state"], tone), a["severity"], a["name"], " ".join(f"{k}={v}" for k, v in a["labels"].items()), shown])
    if not rows:
        return print(c("no alerts", "ok"))
    table(["state", "severity", "alert", "labels", "value"], rows)


def cmd_rules(args):
    if args.json:
        return out({"rules": alerting.list_rules()}, True)
    rows = [[r["name"], r["severity"], f"{r['for']}s", r["expr"][:60]] for r in alerting.list_rules()]
    table(["rule", "severity", "for", "expression"], rows)


def _incident_id(ref: str) -> int:
    """Accept '12', 'INC-0012' or 'inc-12'."""
    m = re.search(r"(\d+)\s*$", str(ref))
    if not m:
        raise SystemExit(f"not an incident reference: {ref}")
    return int(m.group(1))


def cmd_incidents(args):
    if args.declare:
        res = incidents.declare(args.declare, args.severity, "major", args.commander)
        return print(f"{res['reference']} declared (id {res['id']})")
    if args.resolve:
        incidents.update_status(_incident_id(args.resolve), "resolved", "cli", "Resolved from CLI")
        return print("resolved")
    if args.json:
        return out({"incidents": incidents.list_incidents()}, True)
    rows = [[i["reference"], i["severity"], i["status"], i["title"][:54], i["commander"]] for i in incidents.list_incidents()]
    if not rows:
        return print("no incidents")
    table(["ref", "sev", "status", "title", "commander"], rows)


def cmd_slos(args):
    if args.json:
        return out({"slos": slo.list_slos()}, True)
    rows = []
    for s in slo.list_slos():
        burn = s.get("burn_rate_1h")
        tone = "crit" if (s.get("availability") or 1) < s["objective"] else "ok"
        rows.append([s["name"], f"{s['objective'] * 100:.2f}%", f"{(s.get('availability') or 0) * 100:.3f}%",
                     f"{(s.get('budget_remaining_pct') or 0):.1f}%", f"{burn:.2f}x" if burn is not None else "—", c(s.get("status") or "—", tone)])
    table(["slo", "objective", "actual", "budget left", "burn 1h", "status"], rows, ["", "right", "right", "right", "right", ""])


def cmd_checks(args):
    if args.run:
        rows = db.connect().execute("SELECT * FROM checks WHERE id=?", (int(args.run),)).fetchone()
        if not rows:
            raise SystemExit("check not found")
        print(json.dumps(synthetics.check_once(dict(rows), "cli"), indent=2))
        return
    if args.json:
        return out({"checks": synthetics.list_checks()}, True)
    rows = [[ch["name"], ch["url"][:56], f"{ch['uptime_24h']['uptime_pct']:.2f}%",
             f"{ch['last']['latency_ms']:.0f}ms" if ch["last"] and ch["last"]["latency_ms"] else "—",
             c("up", "ok") if ch["last"] and ch["last"]["ok"] else c("down", "crit") if ch["last"] else "never"] for ch in synthetics.list_checks()]
    table(["check", "url", "uptime 24h", "latency", "state"], rows, ["", "", "right", "right", ""])


_CLIENT = {"token": ""}


def _http_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    token = _CLIENT["token"] or os.environ.get("CRAFT_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def cmd_write(args):
    tick = int(time.time() * 1000)
    payload = {"series": [{"metric": args.metric, "labels": dict(x.split("=", 1) for x in args.label), "values": [[tick, args.value]]}]}
    req = urllib.request.Request(
        args.url.rstrip("/") + "/api/metrics/write",
        data=json.dumps(payload).encode(),
        headers=_http_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(resp.read().decode())


def cmd_send_log(args):
    body = sys.stdin.read().strip() if args.stdin else args.body
    if not body:
        raise SystemExit("provide --body or --stdin")
    payload = [{"ts": int(time.time()), "service": args.service, "level": args.level, "body": body}]
    req = urllib.request.Request(
        args.url.rstrip("/") + "/api/logs",
        data=json.dumps(payload).encode(),
        headers=_http_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        print(resp.read().decode())


def cmd_load(args):
    res = loadtest.start(args.name, args.target, args.vus, args.duration, True, args.method)
    print(f"load test #{res['id']} running: {res['vus']} VUs for {res['duration_s']}s")
    while True:
        time.sleep(2)
        tests = loadtest.list_tests(3)
        t = next((x for x in tests if x["id"] == res["id"]), None)
        if t and t["status"] != "running":
            print(json.dumps(t["summary"], indent=2))
            break


def cmd_export(args):
    from .server import export_snapshot

    data = export_snapshot()
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
        print(f"wrote {args.out}")
    else:
        print(json.dumps(data, default=str)[:2000])


def cmd_prune(args):
    print(json.dumps(store.prune(args.days), indent=2))


COMMANDS = (
    "serve", "seed", "status", "query", "metrics", "labels", "logs", "traces", "alerts",
    "rules", "incidents", "slos", "checks", "write", "send-log", "load", "export", "prune",
)


def build_parser() -> argparse.ArgumentParser:
    def _new(name, **kwargs):
        parser = sub.add_parser(name, **kwargs)
        # every subcommand accepts the credential so it can sit next to the other flags
        parser.add_argument(
            "--token", default=argparse.SUPPRESS,
            help="bearer token for outbound commands (or CRAFT_TOKEN)",
        )
        return parser

    ap = argparse.ArgumentParser(prog="craft", description="TelemetryCraft — one lightweight telemetry stack.")
    ap.add_argument("--db", help="path to the SQLite database (default data/telemetry.db)")
    ap.add_argument("--token", help="bearer token for outbound commands (or CRAFT_TOKEN)")
    sub = ap.add_subparsers(dest="command")


    p = _new("serve", help="run the server and collector")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--no-seed", action="store_true")
    p.add_argument("--auth-token", help="require this bearer token (or CRAFT_AUTH_TOKEN)")
    p.add_argument("--auth-user", help="require HTTP basic auth with this user")
    p.add_argument("--auth-password", help="password for --auth-user")
    p.add_argument("--no-auth", action="store_true", help="disable auth even if the environment enables it")
    p.add_argument("--tls-cert", help="PEM certificate (or CRAFT_TLS_CERT)")
    p.add_argument("--tls-key", help="PEM private key (or CRAFT_TLS_KEY)")
    p.add_argument("--tls-client-ca", help="require client certificates signed by this CA")
    p.set_defaults(func=cmd_serve)

    p = _new("seed", help="generate demo telemetry")
    p.add_argument("--force", action="store_true")
    p.add_argument("--self-url", default="http://127.0.0.1:8787")
    p.set_defaults(func=cmd_seed)

    sub.add_parser("status", help="storage and health summary").set_defaults(func=cmd_status)

    p = _new("query", help="run a PromQL expression")
    p.add_argument("expr")
    p.add_argument("--range", help="e.g. 1h, 7d — omit for an instant query")
    p.add_argument("--step", type=int, default=60)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    sub.add_parser("metrics", help="list ingested metrics").set_defaults(func=cmd_metrics)

    p = _new("labels", help="list label names or values")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_labels)

    p = _new("logs", help="search logs")
    p.add_argument("query", nargs="?", default='{service=~".+"}')
    p.add_argument("--since", default="1h")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = _new("traces", help="search traces or print one")
    p.add_argument("trace_id", nargs="?")
    p.add_argument("--service", default="")
    p.add_argument("--status", default="")
    p.add_argument("--min-duration", type=float, default=0)
    p.add_argument("--since", default="1h")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_traces)

    p = _new("alerts", help="list or evaluate alerts")
    p.add_argument("--json", action="store_true")
    p.add_argument("--evaluate", action="store_true")
    p.add_argument("--ack", help="alert instance id")
    p.add_argument("--by", default="cli")
    p.set_defaults(func=cmd_alerts)

    p = _new("rules", help="list alert rules")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_rules)

    p = _new("incidents", help="list, declare or resolve incidents")
    p.add_argument("--json", action="store_true")
    p.add_argument("--declare")
    p.add_argument("--severity", default="SEV3")
    p.add_argument("--commander", default="")
    p.add_argument("--resolve")
    p.set_defaults(func=cmd_incidents)

    p = _new("slos", help="error budgets and burn rates")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_slos)

    p = _new("checks", help="synthetic checks")
    p.add_argument("--json", action="store_true")
    p.add_argument("--run")
    p.set_defaults(func=cmd_checks)

    p = _new("write", help="write one metric to a running server")
    p.add_argument("metric")
    p.add_argument("value", type=float)
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--url", default="http://127.0.0.1:8787")
    p.set_defaults(func=cmd_write)

    p = _new("send-log", help="send a log line to a running server")
    p.add_argument("--service", default="cli")
    p.add_argument("--level", default="info")
    p.add_argument("--body", default="")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--url", default="http://127.0.0.1:8787")
    p.set_defaults(func=cmd_send_log)

    p = _new("load", help="run a bounded load test")
    p.add_argument("--target", required=True)
    p.add_argument("--name", default="cli load test")
    p.add_argument("--vus", type=int, default=5)
    p.add_argument("--duration", type=int, default=20)
    p.add_argument("--method", default="GET")
    p.set_defaults(func=cmd_load)

    p = _new("export", help="export an API-shaped snapshot")
    p.add_argument("--out")
    p.set_defaults(func=cmd_export)

    p = _new("prune", help="apply retention now")
    p.add_argument("--days", type=int)
    p.set_defaults(func=cmd_prune)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.db:
        os.environ["CRAFT_DB"] = args.db
    if getattr(args, "token", None):
        _CLIENT["token"] = args.token
    if not getattr(args, "func", None):
        ap.print_help()
        return
    try:
        args.func(args)
    except urllib.error.HTTPError as exc:
        hint = " (the server wants a credential: pass --token)" if exc.code == 401 else ""
        raise SystemExit(f"craft: {exc.code} {exc.reason}{hint}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"craft: cannot reach the server — {exc.reason}")


if __name__ == "__main__":
    main()
