"""SLOs and error budgets computed from the same time series."""
from __future__ import annotations

import re

from . import db, promql

DUR = re.compile(r"\[(\d+(?:ms|s|m|h|d|w|y))(:\d+(?:ms|s|m|h|d|w|y))?\]")


def _ms_to_duration(ms: int) -> str:
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)):
        if ms % size == 0 and ms >= size:
            return f"{ms // size}{unit}"
    return f"{max(1, ms // 1000)}s"


def rewrite_window(expr: str, step_ms: int) -> str:
    """Widen range selectors so each evaluation point integrates the whole step.

    Without this a 30-day SLO sampled every 6h would only look at the last 5
    minutes of each step and massively over-weight the most recent incident.
    """
    if step_ms <= 300_000:
        return expr

    def repl(match):
        try:
            current = promql.parse_duration(match.group(1))
        except promql.ParseError:
            return match.group(0)
        if current >= step_ms:
            return match.group(0)
        return "[" + _ms_to_duration(int(step_ms)) + "]"

    return DUR.sub(repl, expr)


def list_slos() -> list[dict]:
    rows = db.connect().execute("SELECT * FROM slos ORDER BY id").fetchall()
    out = []
    for r in rows:
        item = {
            "id": r["id"],
            "name": r["name"],
            "service": r["service"],
            "objective": r["objective"],
            "window_days": r["window_days"],
            "sli_expr": r["sli_expr"],
            "total_expr": r["total_expr"],
            "description": r["description"],
        }
        try:
            item.update(compute(item))
        except Exception as exc:  # noqa: BLE001 - a broken expr should not break the page
            item.update({"error": str(exc), "availability": None})
        out.append(item)
    return out


def upsert_slo(slo: dict) -> int:
    conn = db.connect()
    promql.parse(slo["sli_expr"])
    promql.parse(slo["total_expr"])
    row = conn.execute("SELECT id FROM slos WHERE name=?", (slo["name"],)).fetchone()
    values = (
        slo["service"],
        float(slo["objective"]),
        int(slo.get("window_days", 30)),
        slo["sli_expr"],
        slo["total_expr"],
        slo.get("description", ""),
    )
    if row:
        conn.execute("UPDATE slos SET service=?, objective=?, window_days=?, sli_expr=?, total_expr=?, description=? WHERE id=?", (*values, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO slos(name, service, objective, window_days, sli_expr, total_expr, description) VALUES(?,?,?,?,?,?,?)",
        (slo["name"], *values),
    )
    return cur.lastrowid


def _series_sum(expr: str, start_ms: int, end_ms: int, step_ms: int) -> dict[int, float]:
    res = promql.query_range(expr, start_ms, end_ms, step_ms)
    totals: dict[int, float] = {}
    for series in res["result"]:
        for ts, val in series["values"]:
            v = float(val)
            if v != v:
                continue
            totals[int(ts * 1000)] = totals.get(int(ts * 1000), 0.0) + v
    return totals


def compute(slo: dict, points: int = 120) -> dict:
    window_days = slo["window_days"]
    now_ms = db.now() * 1000
    start_ms = now_ms - window_days * 86400 * 1000
    step_ms = max(int((now_ms - start_ms) / points), 60_000)
    step_ms = max(step_ms, 60_000)
    good_expr = rewrite_window(slo["sli_expr"], step_ms)
    total_expr = rewrite_window(slo["total_expr"], step_ms)
    good_rates = _series_sum(good_expr, start_ms, now_ms, step_ms)
    total_rates = _series_sum(total_expr, start_ms, now_ms, step_ms)
    ticks = sorted(set(good_rates) & set(total_rates))
    if not ticks:
        return {"availability": None, "error": "no data in window"}
    sum_good = sum(max(good_rates[t], 0) for t in ticks)
    sum_total = sum(max(total_rates[t], 0) for t in ticks)
    if sum_total <= 0:
        return {"availability": None, "error": "total rate is zero"}
    availability = sum_good / sum_total
    objective = slo["objective"]
    allowed_bad = 1 - objective
    budget_used = (1 - availability) / allowed_bad if allowed_bad > 0 else 0
    history = []
    for t in ticks:
        total = max(total_rates[t], 0)
        good = max(good_rates[t], 0)
        ratio = good / total if total else None
        history.append({"ts": t // 1000, "availability": round(ratio, 4) if ratio is not None else None, "bad_rate": round(1 - ratio, 4) if ratio is not None else None})
    burn_1h = _burn(slo, now_ms, 3600_000)
    burn_6h = _burn(slo, now_ms, 6 * 3600_000)
    burn_24h = _burn(slo, now_ms, 24 * 3600_000)
    return {
        "availability": round(availability, 6),
        "objective": objective,
        "budget_total_pct": round(allowed_bad * 100, 3),
        "budget_used_pct": round(min(budget_used, 99.999) * 100, 3),
        "budget_remaining_pct": round(max(0.0, 1 - budget_used) * 100, 3),
        "status": "healthy" if availability >= objective else "breached",
        "burn_rate_1h": round(burn_1h, 2),
        "burn_rate_6h": round(burn_6h, 2),
        "burn_rate_24h": round(burn_24h, 2),
        "error_budget_minutes": round((1 - availability) * window_days * 24 * 60, 1),
        "allowed_bad_minutes": round(allowed_bad * window_days * 24 * 60, 1),
        "history": history,
        "window_days": window_days,
    }


def _burn(slo: dict, now_ms: int, span_ms: int) -> float:
    step_ms = max(span_ms // 20, 60_000)
    good = _series_sum(rewrite_window(slo["sli_expr"], step_ms), now_ms - span_ms, now_ms, step_ms)
    total = _series_sum(rewrite_window(slo["total_expr"], step_ms), now_ms - span_ms, now_ms, step_ms)
    ticks = sorted(set(good) & set(total))
    if not ticks:
        return 0.0
    g = sum(max(good[t], 0) for t in ticks)
    t = sum(max(total[t], 0) for t in ticks)
    if t <= 0:
        return 0.0
    bad_ratio = 1 - g / t
    allowed = 1 - slo["objective"]
    return bad_ratio / allowed if allowed > 0 else 0.0


def delete_slo(slo_id: int) -> None:
    db.connect().execute("DELETE FROM slos WHERE id=?", (slo_id,))
