"""A PromQL engine: lexer, parser and evaluator.

Covers the query surface dashboards and SRE runbooks actually use: selectors,
range selectors, offset/@, rates and increases, *_over_time, aggregations with
by/without, topk, binary operators with vector matching, comparison filters and
the common math functions.
"""
from __future__ import annotations

import bisect

import math
import re

from . import db

METRIC_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
DURATION_UNITS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000, "y": 31_536_000_000}

AGGREGATORS = {
    "sum", "avg", "min", "max", "count", "stddev", "stdvar", "quantile", "group",
    "topk", "bottomk", "count_values", "limitk", "limit_ratio",
}

FUNCTIONS = {
    # range-vector functions
    "rate", "irate", "increase", "delta", "idelta", "deriv", "resets", "changes", "avg_over_time",
    "min_over_time", "max_over_time", "sum_over_time", "count_over_time", "last_over_time",
    "quantile_over_time", "stddev_over_time", "stdvar_over_time", "absent_over_time",
    "predict_linear", "present_over_time",
    # instant-vector functions
    "abs", "ceil", "floor", "round", "sqrt", "exp", "ln", "log2", "log10", "sgn", "clamp",
    "clamp_min", "clamp_max", "sort", "sort_desc", "sort_by_label", "timestamp", "absent",
    "histogram_quantile", "label_replace", "label_join", "vector", "scalar", "time", "pi",
    "day_of_week", "hour", "minute", "month", "year", "days_in_month",
}


class ParseError(ValueError):
    pass


# --------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------
TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<duration>\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y)(?:\d+(?:\.\d+)?(?:ms|s|m|h|d|w|y))*)
  | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<op>==|!=|=~|!~|<=|>=|\+|-|\*|/|%|\^|==|!=|<|>|=|,|\(|\)|\{|\}|\[|\]|@|:)
  | (?P<ident>[a-zA-Z_][a-zA-Z0-9_]*)
    """,
    re.VERBOSE,
)


class Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind, value, pos):
        self.kind, self.value, self.pos = kind, value, pos

    def __repr__(self):
        return f"Token({self.kind},{self.value!r})"


def _merge_colon_names(tokens: list[Token]) -> list[Token]:
    """Re-join metric names that contain colons (`node:node_cpu:rate5m`).

    A lone `:` is a real token — it separates the range from the resolution in
    a subquery (`[5m:1m]`) — so colons are lexed separately and only glued back
    when they sit between two identifier fragments with no whitespace.
    """
    out: list[Token] = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind == "ident" or (tok.kind == "op" and tok.value == ":"):
            name = tok.value if tok.kind == "ident" else ""
            start = tok.pos
            end = tok.pos + len(tok.value)
            j = i
            while j + 2 < n:
                colon, after = tokens[j + 1], tokens[j + 2]
                glued = colon.kind == "op" and colon.value == ":" and colon.pos == end
                if glued and after.kind == "ident" and after.pos == colon.pos + 1:
                    name += ":" + after.value
                    end = after.pos + len(after.value)
                    j += 2
                    continue
                break
            if name:
                out.append(Token("ident", name, start))
                i = j + 1
                continue
        out.append(tok)
        i += 1
    return out


def lex(src: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        m = TOKEN_RE.match(src, i)
        if not m:
            raise ParseError(f"unexpected character {src[i]!r} at position {i}")
        kind = m.lastgroup
        value = m.group()
        if kind != "ws":
            if kind == "string":
                quote = value[0]
                value = value[1:-1].replace("\\" + quote, quote).replace("\\n", "\n").replace("\\\\", "\\")
            tokens.append(Token(kind, value, i))
        i = m.end()
    tokens.append(Token("eof", "", n))
    return _merge_colon_names(tokens)


def parse_duration(text: str) -> int:
    if text == "":
        raise ParseError("empty duration")
    pos, total, matched = 0, 0.0, False
    for m in DURATION_PART.finditer(text):
        if m.start() != pos:
            raise ParseError(f"bad duration {text!r}")
        total += float(m.group(1)) * DURATION_UNITS[m.group(2)]
        pos = m.end()
        matched = True
    if not matched or pos != len(text):
        raise ParseError(f"bad duration {text!r}")
    return int(total)


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------
class Node:
    pass


class Number(Node):
    def __init__(self, v):
        self.v = float(v)


class String(Node):
    def __init__(self, v):
        self.v = v


class Selector(Node):
    def __init__(self, metric, matchers, range_ms=None, offset_ms=0, at_ms=None):
        self.metric, self.matchers = metric, matchers
        self.range_ms, self.offset_ms, self.at_ms = range_ms, offset_ms, at_ms


class Subquery(Node):
    """`expr[range:step]` — evaluate `expr` on a grid inside a range.

    Used to feed aggregations into range functions, e.g.
    `rate(sum(rate(http_requests_total[1m]))[5m:15s])`.
    """

    def __init__(self, expr, range_ms, step_ms=None, offset_ms=0, at_ms=None):
        self.expr, self.range_ms, self.step_ms = expr, range_ms, step_ms
        self.offset_ms, self.at_ms = offset_ms, at_ms


class Unary(Node):
    def __init__(self, op, expr):
        self.op, self.expr = op, expr


class Binary(Node):
    def __init__(self, op, lhs, rhs, bool_mod=False, on=None, ignoring=None, group=None):
        self.op, self.lhs, self.rhs = op, lhs, rhs
        self.bool_mod, self.on, self.ignoring, self.group = bool_mod, on, ignoring, group


class Call(Node):
    def __init__(self, name, args):
        self.name, self.args = name, args


class Agg(Node):
    def __init__(self, op, expr, param=None, by=None, without=None):
        self.op, self.expr, self.param, self.by, self.without = op, expr, param, by, without


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.tokens = lex(src)
        self.i = 0

    # token helpers
    def peek(self, k=0) -> Token:
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def next(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def accept(self, value) -> bool:
        if self.peek().value == value and self.peek().kind in ("op", "ident"):
            self.i += 1
            return True
        return False

    def expect(self, value) -> Token:
        tok = self.next()
        if tok.value != value:
            raise ParseError(f"expected {value!r}, got {tok.value!r}")
        return tok

    def expect_kind(self, kind) -> Token:
        tok = self.next()
        if tok.kind != kind:
            raise ParseError(f"expected {kind}, got {tok.value!r}")
        return tok

    # grammar
    def parse(self) -> Node:
        node = self.parse_expr()
        if self.peek().kind != "eof":
            raise ParseError(f"unexpected token {self.peek().value!r}")
        return node

    def parse_expr(self) -> Node:
        return self.parse_or()

    def parse_or(self):
        node = self.parse_and()
        while self.peek().value in ("or", "unless") and self.peek().kind == "ident":
            op = self.next().value
            node = Binary(op, node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_comparison()
        while self.peek().value in ("and",) and self.peek().kind == "ident":
            op = self.next().value
            node = Binary(op, node, self.parse_comparison())
        return node

    def parse_comparison(self):
        node = self.parse_additive()
        while self.peek().value in ("==", "!=", "<", "<=", ">", ">="):
            op = self.next().value
            node = self.parse_binary_modifiers(op, node, self.parse_additive())
        return node

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.peek().value in ("+", "-"):
            op = self.next().value
            node = self.parse_binary_modifiers(op, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self):
        node = self.parse_unary()
        while self.peek().value in ("*", "/", "%"):
            op = self.next().value
            node = self.parse_binary_modifiers(op, node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.peek().value in ("-", "+"):
            op = self.next().value
            return Unary(op, self.parse_unary())
        return self.parse_power()

    def parse_power(self):
        node = self.parse_postfix()
        if self.peek().value == "^":
            self.next()
            node = Binary("^", node, self.parse_unary())
        return node

    def parse_binary_modifiers(self, op, lhs, rhs):
        bool_mod = False
        if self.peek().value == "bool":
            self.next()
            bool_mod = True
        node = Binary(op, lhs, rhs, bool_mod=bool_mod)
        if self.peek().value in ("on", "ignoring"):
            node.ignoring = self.next().value == "ignoring"
            node.on = self.parse_label_list()
        if self.peek().value == "group_left" or self.peek().value == "group_right":
            node.group = self.next().value
            node.group_labels = self.parse_label_list() if self.peek().value == "(" else []
        return node

    def parse_label_list(self):
        self.expect("(")
        labels = []
        while not self.accept(")"):
            labels.append(self.expect_kind("ident").value)
            if not self.accept(","):
                self.expect(")")
                break
        return labels

    def parse_postfix(self):
        node = self.parse_primary()
        # selector suffixes
        while True:
            tok = self.peek()
            if tok.value == "[":
                self.next()
                dur = self.expect_kind("duration").value
                if self.peek().value == ":":  # subquery, not a range selector
                    self.next()
                    step = None
                    if self.peek().kind == "duration":
                        step = parse_duration(self.next().value)
                    self.expect("]")
                    node = Subquery(node, parse_duration(dur), step)
                else:
                    self.expect("]")
                    if not isinstance(node, Selector):
                        raise ParseError("a range selector only applies to a metric selector")
                    node.range_ms = parse_duration(dur)
            elif tok.value in ("offset",):
                self.next()
                sign = 1
                if self.peek().value in ("-", "+"):
                    sign = -1 if self.next().value == "-" else 1
                dur = self.expect_kind("duration").value
                if not isinstance(node, (Selector, Subquery)):
                    raise ParseError("offset requires a selector or a subquery")
                node.offset_ms = sign * parse_duration(dur)
            elif tok.value == "@":
                self.next()
                if not isinstance(node, (Selector, Subquery)):
                    raise ParseError("@ requires a selector or a subquery")
                if self.peek().value == "start()" or (self.peek().value == "start" and self.peek(1).value == "("):
                    self.i += 3
                    node.at_ms = -1
                elif self.peek().value == "end" and self.peek(1).value == "(":
                    self.i += 3
                    node.at_ms = -2
                else:
                    ts = self.expect_kind("number").value
                    node.at_ms = int(float(ts) * 1000)
            else:
                break
        return node

    def parse_primary(self):
        tok = self.peek()
        if tok.kind == "number":
            self.next()
            return Number(tok.value)
        if tok.kind == "duration":
            self.next()
            return Number(parse_duration(tok.value) / 1000.0)
        if tok.kind == "string":
            self.next()
            return String(tok.value)
        if tok.value == "(":
            self.next()
            node = self.parse_expr()
            self.expect(")")
            return node
        if tok.value == "{" or (tok.kind == "ident" and tok.value not in FUNCTIONS and tok.value not in AGGREGATORS and tok.value not in ("on", "ignoring", "group_left", "group_right", "bool", "offset", "and", "or", "unless", "by", "without")):
            return self.parse_selector()
        if tok.kind == "ident" and tok.value in AGGREGATORS:
            return self.parse_aggregation()
        if tok.kind == "ident" and tok.value in FUNCTIONS:
            name = self.next().value
            self.expect("(")
            args = []
            while not self.accept(")"):
                args.append(self.parse_expr())
                if not self.accept(","):
                    self.expect(")")
                    break
            return Call(name, args)
        if tok.value == "{":
            return self.parse_selector()
        raise ParseError(f"unexpected token {tok.value!r}")

    def parse_selector(self):
        metric = ""
        tok = self.peek()
        if tok.kind == "ident":
            metric = self.next().value
        matchers = []
        if self.peek().value == "{":
            self.next()
            while not self.accept("}"):
                label = self.expect_kind("ident").value
                op = self.next().value
                if op not in ("=", "!=", "=~", "!~"):
                    raise ParseError(f"bad label operator {op!r}")
                val = self.expect_kind("string").value
                matchers.append((label, op, val))
                if not self.accept(","):
                    self.expect("}")
                    break
        if not metric and not matchers:
            raise ParseError("empty selector")
        return Selector(metric, matchers)

    def parse_aggregation(self):
        name = self.next().value
        by = without = None
        param = None
        if self.peek().value in ("by", "without"):
            mode = self.next().value
            labels = self.parse_label_list()
            if mode == "by":
                by = labels
            else:
                without = labels
        self.expect("(")
        if name in ("topk", "bottomk", "quantile", "count_values", "limitk", "limit_ratio"):
            param = self.parse_expr()
            self.expect(",")
        expr = self.parse_expr()
        self.expect(")")
        if self.peek().value in ("by", "without"):
            mode = self.next().value
            labels = self.parse_label_list()
            if mode == "by":
                by = labels
            else:
                without = labels
        return Agg(name, expr, param, by, without)


def parse(src: str) -> Node:
    if not src or not src.strip():
        raise ParseError("empty query")
    return Parser(src).parse()


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
class Sample:
    __slots__ = ("labels", "value")

    def __init__(self, labels, value):
        self.labels, self.value = labels, value


class Series:
    __slots__ = ("labels", "values")

    def __init__(self, labels, values):
        self.labels, self.values = labels, values


class EvalContext:
    """One evaluation instant. A range query threads a shared `cache` through
    every step so the series list and each selector's sample window are read
    from SQLite once instead of once per step."""

    def __init__(self, eval_ms, start_ms, end_ms, step_ms, cache=None, cover=None):
        self.eval_ms, self.start_ms, self.end_ms, self.step_ms = eval_ms, start_ms, end_ms, step_ms
        self.cache = cache
        # The window a prefetch is allowed to read. For range queries this is the
        # whole query range (so every step shares one SQL read); for an instant
        # query it is widened by the expression's maximum lookback, which is what
        # makes subqueries see data older than the evaluation instant.
        self.cover = cover or (start_ms, end_ms)


_series_cache = {"stamp": None, "rows": []}


def all_series() -> list[dict]:
    stamp = db.get_meta("series_version", 0)
    if _series_cache["stamp"] == stamp:
        return _series_cache["rows"]
    rows = db.connect().execute("SELECT id, metric, labels, type FROM series").fetchall()
    out = []
    for r in rows:
        labels = db.jloads(r["labels"], {})
        labels["__name__"] = r["metric"]
        out.append({"id": r["id"], "labels": labels, "type": r["type"]})
    _series_cache.update(stamp=stamp, rows=out)
    return out


def bump_series_version() -> None:
    with db._LOCK:
        db.set_meta("series_version", (db.get_meta("series_version", 0) or 0) + 1)


def match_labels(labels: dict, matchers) -> bool:
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


def _selector_key(metric: str, matchers) -> tuple:
    return (metric, tuple((m[0], m[1], m[2]) for m in (matchers or [])))


def select_series(metric: str, matchers, cache: dict | None = None) -> list[dict]:
    key = ("series", _selector_key(metric, matchers))
    if cache is not None and key in cache:
        return cache[key]
    out = []
    for s in all_series():
        if metric and s["labels"].get("__name__") != metric:
            continue
        if match_labels(s["labels"], matchers):
            out.append(s)
    if cache is not None:
        cache[key] = out
    return out


def read_samples(ids, start_s: int, end_s: int) -> dict[int, list[tuple[int, float]]]:
    if not ids:
        return {}
    conn = db.connect()
    out: dict[int, list[tuple[int, float]]] = {}
    ids = list(ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = f"SELECT series_id, ts, value FROM samples WHERE series_id IN ({','.join('?' * len(chunk))}) AND ts>=? AND ts<=? ORDER BY ts"
        for row in conn.execute(q, (*chunk, start_s, end_s)):
            out.setdefault(row["series_id"], []).append((row["ts"], row["value"]))
    return out


LOOKBACK_MS = 300_000


def _resolve_time(node_ctx_ms: int, at_ms, start_ms, end_ms) -> int:
    if at_ms is None:
        return node_ctx_ms
    if at_ms == -1:
        return start_ms
    if at_ms == -2:
        return end_ms
    return at_ms


def _prefetched(sel: Selector, ctx: EvalContext):
    """Read one selector's whole window once, so a range query does not hit
    SQLite once per step. Returns (meta_by_id, {sid: (ts_list, value_list)})."""
    cover_start, cover_end = ctx.cover
    key = ("window", _selector_key(sel.metric, sel.matchers), sel.offset_ms, sel.range_ms or 0, cover_start, cover_end)
    hit = ctx.cache.get(key)
    if hit is not None:
        return hit
    series_meta = select_series(sel.metric, sel.matchers, ctx.cache)
    meta_by_id = {s["id"]: s for s in series_meta}
    rng = sel.range_ms or 0
    first = _resolve_time(cover_start, sel.at_ms, ctx.start_ms, ctx.end_ms) - sel.offset_ms - rng
    last = _resolve_time(cover_end, sel.at_ms, ctx.start_ms, ctx.end_ms) - sel.offset_ms
    data = read_samples(meta_by_id.keys(), int(first // 1000), int(last // 1000) + 1)
    packed = {}
    for sid, values in data.items():
        packed[sid] = ([ts for ts, _ in values], [v for _, v in values])
    entry = (meta_by_id, packed)
    ctx.cache[key] = entry
    return entry


def eval_selector(sel: Selector, ctx: EvalContext, want_matrix: bool):
    base = _resolve_time(ctx.eval_ms, sel.at_ms, ctx.start_ms, ctx.end_ms)
    base -= sel.offset_ms
    if ctx.cache is not None and (want_matrix or sel.range_ms):
        rng = sel.range_ms or 0
        meta_by_id, packed = _prefetched(sel, ctx)
        lo = int((base - rng) // 1000)
        hi = int(base // 1000)
        out = []
        for sid, (stamps, values) in packed.items():
            i = bisect.bisect_left(stamps, lo)
            j = bisect.bisect_right(stamps, hi)
            if i >= j:
                continue
            out.append(Series(dict(meta_by_id[sid]["labels"]), [(stamps[k] * 1000, values[k]) for k in range(i, j)]))
        return out

    series_meta = select_series(sel.metric, sel.matchers, ctx.cache)
    if not series_meta:
        return []
    meta_by_id = {s["id"]: s for s in series_meta}
    if want_matrix or sel.range_ms:
        rng = sel.range_ms or 0
        start_s = int((base - rng) // 1000)
        end_s = int(base // 1000) + 1
        data = read_samples(meta_by_id.keys(), start_s, end_s)
        out = []
        for sid, values in data.items():
            pts = [(ts * 1000, v) for ts, v in values if ts * 1000 <= base]
            if not pts:
                continue
            labels = dict(meta_by_id[sid]["labels"])
            out.append(Series(labels, pts))
        return out
    # instant: latest sample within lookback
    start_s = int((base - LOOKBACK_MS) // 1000)
    end_s = int(base // 1000) + 1
    data = read_samples(meta_by_id.keys(), start_s, end_s)
    out = []
    for sid, values in data.items():
        pts = [(ts, v) for ts, v in values if ts * 1000 <= base]
        if not pts:
            continue
        out.append(Sample(dict(meta_by_id[sid]["labels"]), pts[-1][1]))
    return out


# ---------------------------- functions ------------------------------------
def _labels_key(labels, skip_name=True, keep=None, drop=None):
    items = []
    for k in sorted(labels):
        if skip_name and k == "__name__":
            continue
        if keep is not None and k not in keep:
            continue
        if drop is not None and k in drop:
            continue
        items.append((k, labels[k]))
    return tuple(items)


def _rate_like(values, range_ms, fn):
    n = len(values)
    if n < 2:
        return 0.0
    if fn == "delta" or fn == "increase" or fn == "rate":
        counter = fn in ("rate", "increase")
        total = 0.0
        prev = values[0][1]
        for _, v in values[1:]:
            d = v - prev
            if counter and d < 0:
                d = v
            total += d
            prev = v
        span = values[-1][0] - values[0][0]
        if fn == "delta":
            return total * (range_ms / span) if span else 0.0
        if fn == "increase":
            # Prometheus: extrapolate to the full range
            return total * (range_ms / span) if span else 0.0
        return total / (range_ms / 1000.0) if range_ms else 0.0
    if fn == "irate" or fn == "idelta":
        (t0, v0), (t1, v1) = values[-2], values[-1]
        d = v1 - v0
        if fn == "irate":
            if d < 0:
                d = v1
            return d / ((t1 - t0) / 1000.0) if t1 > t0 else 0.0
        return d
    if fn == "deriv":
        return _linear_regression_slope(values)
    if fn == "resets":
        return float(sum(1 for i in range(1, n) if values[i][1] < values[i - 1][1]))
    if fn == "changes":
        return float(sum(1 for i in range(1, n) if values[i][1] != values[i - 1][1]))
    if fn == "avg_over_time":
        return sum(v for _, v in values) / n
    if fn == "sum_over_time":
        return sum(v for _, v in values)
    if fn == "min_over_time":
        return min(v for _, v in values)
    if fn == "max_over_time":
        return max(v for _, v in values)
    if fn == "count_over_time":
        return float(n)
    if fn == "last_over_time":
        return values[-1][1]
    if fn == "present_over_time":
        return 1.0
    if fn == "stddev_over_time" or fn == "stdvar_over_time":
        mean = sum(v for _, v in values) / n
        var = sum((v - mean) ** 2 for _, v in values) / n
        return math.sqrt(var) if fn == "stddev_over_time" else var
    if fn == "quantile_over_time":
        raise ParseError("quantile_over_time needs a phi argument")
    return 0.0


def _linear_regression_slope(values):
    n = len(values)
    if n < 2:
        return 0.0
    t0 = values[0][0]
    xs = [(t - t0) / 1000.0 for t, _ in values]
    ys = [v for _, v in values]
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom


def _quantile(phi, values):
    if not values:
        return float("nan")
    values = sorted(values)
    if phi < 0:
        return -math.inf
    if phi > 1:
        return math.inf
    rank = phi * (len(values) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (rank - lo)


def call_function(node: Call, ctx: EvalContext):
    name = node.name
    range_fns = {
        "rate", "irate", "increase", "delta", "idelta", "deriv", "resets", "changes", "avg_over_time",
        "min_over_time", "max_over_time", "sum_over_time", "count_over_time", "last_over_time",
        "stddev_over_time", "stdvar_over_time", "absent_over_time", "predict_linear",
        "present_over_time", "quantile_over_time",
    }
    if name in range_fns:
        arg = node.args[0] if name == "predict_linear" and len(node.args) > 1 else node.args[-1]
        series, range_ms = _range_vector(arg, ctx)
        offset_ms = getattr(arg, "offset_ms", 0) or 0
        out = []
        for s in series:
            values = [(t, v) for t, v in s.values if t <= ctx.eval_ms - offset_ms]
            if not values:
                continue
            if name == "quantile_over_time":
                phi = float(eval_scalar(node.args[0], ctx))
                val = _quantile(phi, [v for _, v in values])
            elif name == "predict_linear":
                secs = float(eval_scalar(node.args[1], ctx))
                slope = _linear_regression_slope(values)
                val = values[-1][1] + slope * secs
            elif name == "absent_over_time":
                continue
            else:
                val = _rate_like(values, range_ms or 1, name)
            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                continue
            out.append(Sample(dict(s.labels), float(val)))
        if name == "absent_over_time" and not series:
            return [Sample(_absent_labels(arg), 1.0)]
        return out

    vec = lambda i=0: eval_expr(node.args[i], ctx)
    if name == "histogram_quantile":
        phi = float(eval_scalar(node.args[0], ctx))
        samples = vec(1)
        if not isinstance(samples, list):
            return []
        groups: dict[tuple, list] = {}
        for s in samples:
            le = s.labels.get("le")
            if le is None:
                continue
            try:
                bound = float(le)
            except ValueError:
                bound = math.inf if le == "+Inf" else None
            if bound is None:
                continue
            key = _labels_key(s.labels, skip_name=True, drop={"le"})
            groups.setdefault(key, []).append((bound, s.value, s.labels))
        out = []
        for key, buckets in groups.items():
            buckets.sort(key=lambda b: b[0])
            if not buckets or buckets[-1][0] != math.inf:
                continue
            total = buckets[-1][1]
            if total <= 0:
                out.append(Sample({k: v for k, v in buckets[0][2].items() if k != "le"}, float("nan")))
                continue
            target = phi * total
            prev_bound, prev_count = 0.0, 0.0
            value = buckets[-1][0]
            found = False
            for bound, count, labels in buckets:
                if count >= target and bound != math.inf:
                    if count == prev_count:
                        value = bound
                    else:
                        value = prev_bound + (bound - prev_bound) * (target - prev_count) / (count - prev_count)
                    found = True
                    break
                prev_bound, prev_count = bound, count
            if not found:
                value = buckets[-2][0] if len(buckets) > 1 else 0.0
            base_labels = {k: v for k, v in buckets[0][2].items() if k != "le"}
            out.append(Sample(base_labels, value))
        return out
    if name == "vector":
        return [Sample({}, float(eval_scalar(node.args[0], ctx)))]
    if name == "scalar":
        v = vec(0)
        if isinstance(v, list) and len(v) == 1:
            return v[0].value
        return float("nan")
    if name == "time":
        return ctx.eval_ms / 1000.0
    if name == "pi":
        return math.pi
    if name == "absent":
        v = vec(0)
        if not v:
            return [Sample(_absent_labels(node.args[0]), 1.0)]
        return []
    if name in ("sort", "sort_desc"):
        v = vec(0)
        return sorted(v, key=lambda s: s.value, reverse=name == "sort_desc")
    if name == "sort_by_label":
        v = vec(0)
        label = node.args[1].v if len(node.args) > 1 and isinstance(node.args[1], String) else ""
        return sorted(v, key=lambda s: s.labels.get(label, ""))
    if name == "timestamp":
        return [Sample(dict(s.labels), ctx.eval_ms / 1000.0) for s in vec(0)]
    if name == "label_replace":
        v = vec(0)
        dst = node.args[1].v
        repl = node.args[2].v
        src = node.args[3].v
        regex = node.args[4].v
        out = []
        for s in v:
            m = re.fullmatch(regex, s.labels.get(src, ""))
            if m:
                new_labels = dict(s.labels)
                new_labels[dst] = m.expand(repl)
                out.append(Sample(new_labels, s.value))
            else:
                out.append(s)
        return out
    if name == "label_join":
        v = vec(0)
        dst, sep = node.args[1].v, node.args[2].v
        srcs = [a.v for a in node.args[3:]]
        return [Sample({**s.labels, dst: sep.join(s.labels.get(x, "") for x in srcs)}, s.value) for s in v]
    if name in ("day_of_week", "hour", "minute", "month", "year", "days_in_month"):
        import datetime as dt
        src = node.args[0] if node.args else None
        ts = eval_scalar(src, ctx) if src else ctx.eval_ms / 1000.0
        d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        return {
            "day_of_week": (d.weekday() + 1) % 7,
            "hour": d.hour,
            "minute": d.minute,
            "month": d.month,
            "year": d.year,
            "days_in_month": __import__("calendar").monthrange(d.year, d.month)[1],
        }[name]

    # element-wise math
    v = vec(0)
    if isinstance(v, (int, float)):
        scalars = [Sample({}, float(v))]
    else:
        scalars = v
    out = []
    for s in scalars:
        x = s.value
        try:
            if name == "abs":
                y = abs(x)
            elif name == "ceil":
                y = math.ceil(x)
            elif name == "floor":
                y = math.floor(x)
            elif name == "sqrt":
                y = math.sqrt(x)
            elif name == "exp":
                y = math.exp(x)
            elif name == "ln":
                y = math.log(x)
            elif name == "log2":
                y = math.log2(x)
            elif name == "log10":
                y = math.log10(x)
            elif name == "sgn":
                y = (x > 0) - (x < 0)
            elif name == "round":
                nd = float(eval_scalar(node.args[1], ctx)) if len(node.args) > 1 else 1
                y = math.floor(x / nd + 0.5) * nd
            elif name == "clamp":
                lo = float(eval_scalar(node.args[1], ctx))
                hi = float(eval_scalar(node.args[2], ctx))
                y = max(lo, min(hi, x))
            elif name == "clamp_min":
                y = max(float(eval_scalar(node.args[1], ctx)), x)
            elif name == "clamp_max":
                y = min(float(eval_scalar(node.args[1], ctx)), x)
            else:
                raise ParseError(f"unsupported function {name}")
        except (ValueError, OverflowError):
            continue
        out.append(Sample(dict(s.labels), float(y)))
    return out


def _absent_labels(arg):
    if isinstance(arg, Selector):
        labels = {m[0]: m[2] for m in arg.matchers if m[1] == "="}
        if arg.metric:
            labels["__name__"] = arg.metric
        labels.pop("__name__", None)
        return labels
    return {}


def eval_scalar(node, ctx) -> float:
    v = eval_expr(node, ctx)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        if len(v) == 1:
            return v[0].value
        raise ParseError("expected scalar")
    raise ParseError("expected scalar")


DEFAULT_SUBQUERY_STEP_MS = 60_000


def lookback_span(node) -> int:
    """How far back an expression can reach, so an instant query can prefetch it."""
    if isinstance(node, Selector):
        return (node.range_ms or LOOKBACK_MS) + abs(node.offset_ms or 0)
    if isinstance(node, Subquery):
        return node.range_ms + abs(node.offset_ms or 0) + lookback_span(node.expr)
    children = []
    for attr in ("expr", "lhs", "rhs", "param"):
        child = getattr(node, attr, None)
        if child is not None:
            children.append(child)
    children.extend(getattr(node, "args", []) or [])
    return max((lookback_span(c) for c in children), default=LOOKBACK_MS)


def eval_subquery(node: Subquery, ctx) -> list[Series]:
    """Evaluate `expr[range:step]` into a range vector.

    The inner expression is evaluated once per step, which is the same thing
    Prometheus does; the cost is (range / step) evaluations, so the default
    resolution is the outer query step and never finer than a minute.
    """
    base = _resolve_time(ctx.eval_ms, node.at_ms, ctx.start_ms, ctx.end_ms) - node.offset_ms
    step = node.step_ms or max(int(ctx.step_ms or 0), DEFAULT_SUBQUERY_STEP_MS)
    step = max(int(step), 1000)
    start = base - node.range_ms
    stamps = list(range(start, base + 1, step))
    if not stamps:
        stamps = [base]
    merged: dict[tuple, tuple[dict, list]] = {}
    for t in stamps:
        inner = EvalContext(t, ctx.start_ms, ctx.end_ms, step, ctx.cache, ctx.cover)
        result = eval_expr(node.expr, inner)
        if isinstance(result, (int, float)):
            result = [Sample({}, float(result))]
        for sample in result:
            key = tuple(sorted(sample.labels.items()))
            entry = merged.setdefault(key, (sample.labels, []))
            entry[1].append((t, sample.value))
    out = []
    for labels, values in merged.values():
        values = [(t, v) for t, v in values if t <= base]
        if values:
            out.append(Series(dict(labels), values))
    return out


def _range_vector(arg, ctx) -> tuple[list, int]:
    """Return (series, range_ms) for a range function argument."""
    if isinstance(arg, Selector):
        if not arg.range_ms:
            raise ParseError("expected a range selector, e.g. metric[5m]")
        return eval_selector(arg, ctx, want_matrix=True), arg.range_ms
    if isinstance(arg, Subquery):
        return eval_subquery(arg, ctx), arg.range_ms
    raise ParseError("expected a range selector or a subquery, e.g. metric[5m] or expr[5m:1m]")


def eval_expr(node, ctx):
    if isinstance(node, Number):
        return node.v
    if isinstance(node, String):
        return node.v
    if isinstance(node, Selector):
        return eval_selector(node, ctx, want_matrix=False)
    if isinstance(node, Subquery):
        # at the top level a subquery behaves like an instant vector of its last point
        return [Sample(dict(s.labels), s.values[-1][1]) for s in eval_subquery(node, ctx)]
    if isinstance(node, Unary):
        v = eval_expr(node.expr, ctx)
        if isinstance(v, (int, float)):
            return -v if node.op == "-" else v
        return [Sample(s.labels, -s.value if node.op == "-" else s.value) for s in v]
    if isinstance(node, Call):
        return call_function(node, ctx)
    if isinstance(node, Agg):
        return eval_aggregation(node, ctx)
    if isinstance(node, Binary):
        return eval_binary(node, ctx)
    raise ParseError("unknown node")


def _group_key(labels, by, without):
    if by is not None:
        return tuple((k, labels.get(k, "")) for k in by)
    if without is not None:
        return tuple((k, v) for k, v in sorted(labels.items()) if k not in without and k != "__name__")
    return ()


def eval_aggregation(node: Agg, ctx):
    param = eval_scalar(node.param, ctx) if node.param is not None else None
    v = eval_expr(node.expr, ctx)
    if isinstance(v, (int, float)):
        v = [Sample({}, float(v))]
    groups: dict[tuple, list[Sample]] = {}
    for s in v:
        key = _group_key(s.labels, node.by, node.without)
        groups.setdefault(key, []).append(s)
    out = []
    for key, samples in groups.items():
        labels = dict(key)
        values = [s.value for s in samples]
        op = node.op
        if op == "sum":
            val = sum(values)
        elif op == "avg":
            val = sum(values) / len(values)
        elif op == "min":
            val = min(values)
        elif op == "max":
            val = max(values)
        elif op == "count":
            val = float(len(values))
        elif op == "group":
            val = 1.0
        elif op == "stddev" or op == "stdvar":
            mean = sum(values) / len(values)
            var = sum((x - mean) ** 2 for x in values) / len(values)
            val = math.sqrt(var) if op == "stddev" else var
        elif op == "quantile":
            val = _quantile(param, values)
        elif op in ("topk", "bottomk"):
            k = int(param or 1)
            ordered = sorted(samples, key=lambda s: s.value, reverse=(op == "topk"))[:k]
            for s in ordered:
                out.append(Sample(dict(s.labels), s.value))
            continue
        elif op == "count_values":
            val = float(len(samples))
        elif op in ("limitk", "limit_ratio"):
            out.extend(samples[: int(param or 1)])
            continue
        else:
            raise ParseError(f"unsupported aggregation {op}")
        out.append(Sample(labels, float(val)))
    return out


def eval_binary(node: Binary, ctx):
    lhs, rhs = eval_expr(node.lhs, ctx), eval_expr(node.rhs, ctx)
    op = node.op
    if isinstance(lhs, str) or isinstance(rhs, str):
        raise ParseError("binary operation on string")

    def apply(a, b):
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if b != 0 else (math.inf if a > 0 else (-math.inf if a < 0 else math.nan))
        if op == "%":
            return math.fmod(a, b) if b else math.nan
        if op == "^":
            return a ** b
        if op in ("==", "!=", "<", "<=", ">", ">="):
            return {
                "==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b,
            }[op]
        raise ParseError(f"bad operator {op}")

    if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
        res = apply(lhs, rhs)
        return float(res) if not isinstance(res, bool) else (1.0 if res else 0.0)

    if isinstance(rhs, (int, float)):
        out = []
        for s in lhs:
            res = apply(s.value, rhs)
            if isinstance(res, bool):
                if node.bool_mod:
                    out.append(Sample(dict(s.labels), 1.0 if res else 0.0))
                elif res:
                    out.append(Sample(dict(s.labels), s.value))
            else:
                labels = {k: v for k, v in s.labels.items() if k != "__name__"}
                out.append(Sample(labels, float(res)))
        return out
    if isinstance(lhs, (int, float)):
        out = []
        for s in rhs:
            res = apply(lhs, s.value)
            if isinstance(res, bool):
                if node.bool_mod:
                    out.append(Sample(dict(s.labels), 1.0 if res else 0.0))
                elif res:
                    out.append(Sample(dict(s.labels), s.value))
            else:
                labels = {k: v for k, v in s.labels.items() if k != "__name__"}
                out.append(Sample(labels, float(res)))
        return out

    # vector <-> vector
    if op in ("and", "or", "unless"):
        lhs_keys = {_labels_key(s.labels): s for s in lhs}
        rhs_keys = {_labels_key(s.labels): s for s in rhs}
        if op == "and":
            return [lhs_keys[k] for k in lhs_keys if k in rhs_keys]
        if op == "or":
            out = list(lhs)
            for k, s in rhs_keys.items():
                if k not in lhs_keys:
                    out.append(s)
            return out
        return [lhs_keys[k] for k in lhs_keys if k not in rhs_keys]

    def key(labels):
        if node.on is not None:
            return tuple((k, labels.get(k, "")) for k in node.on)
        if node.ignoring is not None:
            return tuple((k, v) for k, v in sorted(labels.items()) if k not in node.ignoring and k != "__name__")
        return _labels_key(labels)

    rhs_map: dict[tuple, Sample] = {}
    for s in rhs:
        rhs_map[key(s.labels)] = s
    out = []
    for ls in lhs:
        rs = rhs_map.get(key(ls.labels))
        if rs is None:
            continue
        res = apply(ls.value, rs.value)
        if isinstance(res, bool):
            if node.bool_mod:
                out.append(Sample(dict(rs.labels), 1.0 if res else 0.0))
            elif res:
                out.append(Sample(dict(ls.labels), ls.value))
            continue
        if node.group in ("group_left", "group_right"):
            labels = dict(ls.labels if node.group == "group_left" else rs.labels)
            labels.update({k: rs.labels.get(k, "") for k in getattr(node, "group_labels", []) if node.group == "group_left"})
        else:
            labels = {k: v for k, v in rs.labels.items() if k != "__name__"}
        out.append(Sample(labels, float(res)))
    return out


def _fmt_value(v: float):
    if v != v:
        return "NaN"
    if v == math.inf:
        return "+Inf"
    if v == -math.inf:
        return "-Inf"
    return repr(float(v))


def to_api(result, result_type=None, ts_ms=0):
    if isinstance(result, (int, float)):
        return {"resultType": "scalar", "result": [ts_ms / 1000.0, _fmt_value(float(result))]}
    if not result:
        return {"resultType": result_type or "vector", "result": []}
    if isinstance(result[0], Series):
        return {
            "resultType": "matrix",
            "result": [
                {"metric": {k: v for k, v in s.labels.items()}, "values": [[t / 1000.0, _fmt_value(v)] for t, v in s.values]}
            for s in result
            ],
        }
    return {
        "resultType": result_type or "vector",
        # Prometheus stamps every instant sample with the evaluation time
        "result": [{"metric": dict(s.labels), "value": [ts_ms / 1000.0, _fmt_value(s.value)]} for s in result],
    }


def query_instant(expr: str, time_ms: int):
    ast = parse(expr)
    span = lookback_span(ast)
    ctx = EvalContext(time_ms, time_ms, time_ms, 0, None, (time_ms - span, time_ms))
    result = eval_expr(ast, ctx)
    if isinstance(result, (int, float)):
        return {"resultType": "scalar", "result": [time_ms / 1000.0, _fmt_value(float(result))]}
    if result and isinstance(result[0], Series):
        # a range selector used as an instant query returns a matrix of raw values
        return to_api(result, "matrix", time_ms)
    if not result:
        # Determine intent: range selectors produce matrices
        if isinstance(ast, Selector) and ast.range_ms:
            return to_api([], "matrix", time_ms)
    return to_api(result, None, time_ms)


def query_range(expr: str, start_ms: int, end_ms: int, step_ms: int):
    ast = parse(expr)
    step_ms = max(int(step_ms), 1000)
    points = int((end_ms - start_ms) // step_ms) + 1
    if points <= 0:
        return {"resultType": "matrix", "result": []}
    if points > 20000:
        step_ms = int((end_ms - start_ms) / 20000) or 1000
        points = int((end_ms - start_ms) // step_ms) + 1
    merged: dict[tuple, dict] = {}
    base_labels: dict[tuple, dict] = {}
    cache: dict = {}
    for i in range(points):
        t = start_ms + i * step_ms
        ctx = EvalContext(t, start_ms, end_ms, step_ms, cache)
        result = eval_expr(ast, ctx)
        if isinstance(result, (int, float)):
            result = [Sample({}, float(result))]
        for s in result:
            key = tuple(sorted(s.labels.items()))
            base_labels.setdefault(key, s.labels)
            merged.setdefault(key, []).append((t, s.value))
    out = []
    for key, values in merged.items():
        out.append({"metric": dict(base_labels[key]), "values": [[t / 1000.0, _fmt_value(v)] for t, v in values]})
    out.sort(key=lambda r: tuple(sorted(r["metric"].items())))
    return {"resultType": "matrix", "result": out}


def label_names() -> list[str]:
    names = set()
    for s in all_series():
        names.update(s["labels"].keys())
    names.discard("__name__")
    return sorted(names)


def label_values(name: str) -> list[str]:
    vals = set()
    for s in all_series():
        if name == "__name__":
            vals.add(s["labels"]["__name__"])
        elif name in s["labels"]:
            vals.add(s["labels"][name])
    return sorted(vals)
