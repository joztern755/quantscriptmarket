"""No-code strategy builder: JSON spec → Python source that passes validate.py (SPEC §10).

There is ONE execution path: the generated source is an ordinary strategy script (fixed, audited
interpreter + the spec embedded as literal data) and runs through runner.py like any upload.

Spec (all keys required unless marked optional)::

    {
      "version": 1,                                   # optional, must be 1
      "markets": ["BTC"],                             # 1–5 coins
      "timeframe": "1d",                              # "1h" | "4h" | "1d"
      "lookback": 300,                                # 50–1000 bars
      "max_leverage": 1,                              # 1–5
      "indicators": {                                 # 0–20; id = [a-z][a-z0-9_]{0,31}
        "fast": {"type": "sma", "source": "close", "period": 20},
        "slow": {"type": "ema", "source": "close", "period": 100},
        "hh":   {"type": "highest", "source": "high", "period": 20, "shift": 1}
      },
      "rules": [                                      # 1–20, first matching rule wins
        {"when": {"all": [{"left": "fast", "op": "crosses_above", "right": "slow"}]}, "weight": 1},
        {"when": {"any": [{"left": "close", "op": ">", "right": "hh"},
                          {"left": "rsi14", "op": "<", "right": 30}]}, "weight": 0.5}
      ],
      "default_weight": 0                             # optional, default 0
    }

* indicator ``type``: ``sma`` | ``ema`` | ``rsi`` | ``atr`` | ``highest`` | ``lowest`` | ``roc``;
  ``source`` (not for atr): ``close`` | ``high`` | ``low`` | ``open`` (default ``close``);
  ``period`` 1–500 (rsi/atr/roc ≥ 1, sma/ema ≥ 1); optional ``shift`` 0–50 = value from that many
  bars ago (e.g. ``highest(high, 20, shift=1)`` = highest high of the 20 bars *before* the current one).
  RSI and ATR use Wilder smoothing; EMA is seeded with the SMA of the first ``period`` values;
  ROC = (x / x[n bars ago] − 1) × 100.
* operand (``left``/``right``): an indicator id, a price field of the last closed bar
  (``close``/``open``/``high``/``low``/``volume``), or a number.
* ``op``: ``>`` ``<`` ``>=`` ``<=`` ``crosses_above`` ``crosses_below`` (crosses compare the last two
  closed bars: ``prev_left <= prev_right and left > right``).
* ``when``: ``{"all": [...]}`` or ``{"any": [...]}`` of conditions or nested all/any (depth ≤ 3,
  1–20 items each). A condition whose indicator has no value yet (warm-up) is false.
* Weights apply to **each** market independently (rules are evaluated per market on that market's
  bars), so ``len(markets) × max|weight| ≤ max_leverage`` is required.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from app.errors import ValidationFailed
from app.sandbox.validate import (
    ALLOWED_TIMEFRAMES, MAX_LOOKBACK, MAX_MARKETS, MIN_LOOKBACK, PLATFORM_MAX_LEVERAGE, ensure_valid,
    is_valid_coin,
)

__all__ = ["INDICATOR_TYPES", "OPS", "PRICE_FIELDS", "validate_spec", "compile_spec"]

INDICATOR_TYPES = ("sma", "ema", "rsi", "atr", "highest", "lowest", "roc")
SOURCES = ("close", "high", "low", "open")
PRICE_FIELDS = ("close", "open", "high", "low", "volume")
OPS = (">", "<", ">=", "<=", "crosses_above", "crosses_below")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_INDICATORS, MAX_RULES, MAX_ITEMS, MAX_DEPTH = 20, 20, 20, 3
MAX_PERIOD, MAX_SHIFT = 500, 50
_SPEC_KEYS = {"version", "markets", "timeframe", "lookback", "max_leverage", "indicators", "rules", "default_weight", "name", "description"}


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def validate_spec(spec: Any, *, platform_max_leverage: float = PLATFORM_MAX_LEVERAGE) -> list[dict[str, str]]:
    """Return a list of ``{"path": "rules[0].when.all[1].op", "message": ...}``; empty = valid."""
    errs: list[dict[str, str]] = []

    def e(path: str, msg: str) -> None:
        if len(errs) < 100:
            errs.append({"path": path, "message": msg})

    if not isinstance(spec, dict):
        e("", "spec must be a JSON object")
        return errs
    for k in spec:
        if k not in _SPEC_KEYS:
            e(str(k)[:40], "unknown key")
    if "version" in spec and spec["version"] != 1:
        e("version", "only version 1 is supported")

    markets = spec.get("markets")
    if not isinstance(markets, list) or not (1 <= len(markets) <= MAX_MARKETS):
        e("markets", f"must be a list of 1–{MAX_MARKETS} coin names")
        markets = []
    else:
        for i, c in enumerate(markets):
            if not is_valid_coin(c):
                e(f"markets[{i}]", "must be a Hyperliquid perp coin like 'BTC' or 'xyz:SILVER'")
        if len(set(map(str, markets))) != len(markets):
            e("markets", "duplicate coins")
    if spec.get("timeframe") not in ALLOWED_TIMEFRAMES:
        e("timeframe", f"must be one of {', '.join(ALLOWED_TIMEFRAMES)}")
    lookback = spec.get("lookback")
    if not _is_int(lookback) or not (MIN_LOOKBACK <= lookback <= MAX_LOOKBACK):
        e("lookback", f"must be an integer {MIN_LOOKBACK}–{MAX_LOOKBACK}")
        lookback = MAX_LOOKBACK
    max_lev = spec.get("max_leverage")
    if not _is_num(max_lev) or not (1 <= max_lev <= platform_max_leverage):
        e("max_leverage", f"must be a number 1–{platform_max_leverage:g}")
        max_lev = None

    inds = spec.get("indicators", {})
    ids: set[str] = set()
    if not isinstance(inds, dict) or len(inds) > MAX_INDICATORS:
        e("indicators", f"must be an object with at most {MAX_INDICATORS} entries")
        inds = {}
    for iid, ind in inds.items():
        p = f"indicators.{str(iid)[:40]}"
        if not isinstance(iid, str) or not _ID_RE.match(iid):
            e(p, "id must match [a-z][a-z0-9_]{0,31}")
            continue
        if iid in PRICE_FIELDS:
            e(p, f"id {iid!r} is reserved for the price field")
            continue
        ids.add(iid)
        if not isinstance(ind, dict):
            e(p, "must be an object")
            continue
        for k in ind:
            if k not in ("type", "source", "period", "shift"):
                e(f"{p}.{str(k)[:40]}", "unknown key")
        typ = ind.get("type")
        if typ not in INDICATOR_TYPES:
            e(f"{p}.type", f"must be one of {', '.join(INDICATOR_TYPES)}")
        if typ == "atr":
            if "source" in ind:
                e(f"{p}.source", "atr uses high/low/close; omit source")
        elif ind.get("source", "close") not in SOURCES:
            e(f"{p}.source", f"must be one of {', '.join(SOURCES)}")
        period = ind.get("period")
        if not _is_int(period) or not (1 <= period <= MAX_PERIOD):
            e(f"{p}.period", f"must be an integer 1–{MAX_PERIOD}")
            period = 1
        shift = ind.get("shift", 0)
        if not _is_int(shift) or not (0 <= shift <= MAX_SHIFT):
            e(f"{p}.shift", f"must be an integer 0–{MAX_SHIFT}")
            shift = 0
        if period + shift + 2 > lookback:
            e(p, f"period + shift + 2 must be ≤ lookback ({lookback}) so the value and a crossing can be computed")

    def operand(x: Any, path: str) -> None:
        if isinstance(x, str):
            if x not in ids and x not in PRICE_FIELDS:
                e(path, f"unknown indicator {x[:40]!r} (define it under indicators, or use a price field / number)")
        elif not _is_num(x):
            e(path, "must be an indicator id, a price field, or a finite number")

    def when(node: Any, path: str, depth: int) -> None:
        if not isinstance(node, dict) or len(node) != 1 or next(iter(node)) not in ("all", "any"):
            e(path, "must be {\"all\": [...]} or {\"any\": [...]}")
            return
        key = next(iter(node))
        items = node[key]
        if not isinstance(items, list) or not (1 <= len(items) <= MAX_ITEMS):
            e(f"{path}.{key}", f"must be a list of 1–{MAX_ITEMS} conditions")
            return
        for i, it in enumerate(items):
            ip = f"{path}.{key}[{i}]"
            if isinstance(it, dict) and ("all" in it or "any" in it):
                if depth >= MAX_DEPTH:
                    e(ip, f"nesting deeper than {MAX_DEPTH} is not allowed")
                else:
                    when(it, ip, depth + 1)
                continue
            if not isinstance(it, dict) or set(it) != {"left", "op", "right"}:
                e(ip, "condition must have exactly left, op, right")
                continue
            if it["op"] not in OPS:
                e(f"{ip}.op", f"must be one of {', '.join(OPS)}")
            operand(it["left"], f"{ip}.left")
            operand(it["right"], f"{ip}.right")
            if _is_num(it["left"]) and _is_num(it["right"]):
                e(ip, "at least one side must be an indicator or price field")

    rules = spec.get("rules")
    weights: list[float] = []
    if not isinstance(rules, list) or not (1 <= len(rules) <= MAX_RULES):
        e("rules", f"must be a list of 1–{MAX_RULES} rules")
        rules = []
    for i, r in enumerate(rules):
        p = f"rules[{i}]"
        if not isinstance(r, dict) or set(r) != {"when", "weight"}:
            e(p, "rule must have exactly 'when' and 'weight'")
            continue
        when(r["when"], f"{p}.when", 1)
        if not _is_num(r["weight"]):
            e(f"{p}.weight", "must be a finite number")
        else:
            weights.append(float(r["weight"]))
    dw = spec.get("default_weight", 0)
    if not _is_num(dw):
        e("default_weight", "must be a finite number")
    else:
        weights.append(float(dw))
    if max_lev is not None and markets and weights:
        worst = max(abs(w) for w in weights)
        if worst > max_lev:
            e("rules", f"a weight of {worst:g} exceeds max_leverage {max_lev:g}")
        elif worst * len(markets) > max_lev + 1e-9:
            e("rules", f"weights apply to each of {len(markets)} markets: {len(markets)} × {worst:g} exceeds max_leverage {max_lev:g}")
    return errs


# Fixed interpreter embedded in every generated script. It must itself pass validate.py.
_INTERPRETER = '''

def _src(rows, name):
    key = {"close": "c", "open": "o", "high": "h", "low": "l", "volume": "v"}[name]
    return [b[key] for b in rows]


def _sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2.0 / (n + 1)
    v = sum(xs[:n]) / n
    out[n - 1] = v
    for i in range(n, len(xs)):
        v = xs[i] * k + v * (1 - k)
        out[i] = v
    return out


def _wilder(xs, n, first):
    out = [None] * len(xs)
    if len(xs) < first + n:
        return out
    v = sum(xs[first:first + n]) / n
    out[first + n - 1] = v
    for i in range(first + n, len(xs)):
        v = (v * (n - 1) + xs[i]) / n
        out[i] = v
    return out


def _rsi(xs, n):
    gains = [0.0] * len(xs)
    losses = [0.0] * len(xs)
    for i in range(1, len(xs)):
        d = xs[i] - xs[i - 1]
        gains[i] = d if d > 0 else 0.0
        losses[i] = -d if d < 0 else 0.0
    ag = _wilder(gains, n, 1)
    al = _wilder(losses, n, 1)
    out = [None] * len(xs)
    for i in range(len(xs)):
        if ag[i] is None or al[i] is None:
            continue
        if al[i] == 0:
            out[i] = 100.0 if ag[i] > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag[i] / al[i])
    return out


def _atr(rows, n):
    tr = [0.0] * len(rows)
    for i, b in enumerate(rows):
        if i == 0:
            tr[i] = b["h"] - b["l"]
        else:
            pc = rows[i - 1]["c"]
            tr[i] = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
    return _wilder(tr, n, 1)


def _window(xs, n, pick):
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        out[i] = pick(xs[i - n + 1:i + 1])
    return out


def _roc(xs, n):
    out = [None] * len(xs)
    for i in range(n, len(xs)):
        if xs[i - n] != 0:
            out[i] = (xs[i] / xs[i - n] - 1.0) * 100.0
    return out


def _series(rows, spec):
    typ, source, n, shift = spec
    if typ == "atr":
        s = _atr(rows, n)
    else:
        xs = _src(rows, source)
        if typ == "sma":
            s = _sma(xs, n)
        elif typ == "ema":
            s = _ema(xs, n)
        elif typ == "rsi":
            s = _rsi(xs, n)
        elif typ == "highest":
            s = _window(xs, n, max)
        elif typ == "lowest":
            s = _window(xs, n, min)
        else:
            s = _roc(xs, n)
    if shift:
        s = [None] * shift + s[:len(s) - shift]
    return s


def _value(rows, cache, operand, back):
    kind, ref = operand
    if kind == "num":
        return ref
    if len(rows) < back + 1:
        return None
    if kind == "px":
        return _src(rows, ref)[-1 - back]
    if ref not in cache:
        cache[ref] = _series(rows, INDICATORS[ref])
    return cache[ref][-1 - back]


def _cond(rows, cache, c):
    left, op, right = c
    a = _value(rows, cache, left, 0)
    b = _value(rows, cache, right, 0)
    if a is None or b is None:
        return False
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    pa = _value(rows, cache, left, 1)
    pb = _value(rows, cache, right, 1)
    if pa is None or pb is None:
        return False
    if op == "crosses_above":
        return pa <= pb and a > b
    return pa >= pb and a < b


def _when(rows, cache, node):
    kind, items = node
    for it in items:
        ok = _when(rows, cache, it[1]) if it[0] == "group" else _cond(rows, cache, it[1])
        if kind == "any" and ok:
            return True
        if kind == "all" and not ok:
            return False
    return kind == "all"


def signal(bars):
    out = {}
    for coin in MARKETS:
        rows = bars[coin]
        cache = {}
        w = DEFAULT_WEIGHT
        for node, weight in RULES:
            if _when(rows, cache, node):
                w = weight
                break
        out[coin] = w
    return out
'''


def _operand(x: Any) -> tuple[str, Any]:
    if isinstance(x, str):
        return ("px", x) if x in PRICE_FIELDS else ("ind", x)
    return ("num", float(x))


def _compile_when(node: dict[str, Any]) -> tuple[str, list[Any]]:
    key = next(iter(node))
    items: list[Any] = []
    for it in node[key]:
        if "all" in it or "any" in it:
            items.append(("group", _compile_when(it)))
        else:
            items.append(("cond", (_operand(it["left"]), it["op"], _operand(it["right"]))))
    return (key, items)


def compile_spec(spec: Any, *, platform_max_leverage: float = PLATFORM_MAX_LEVERAGE) -> str:
    """Validate ``spec`` and return strategy source. Raises ValidationFailed(errors=[{path, message}])."""
    errs = validate_spec(spec, platform_max_leverage=platform_max_leverage)
    if errs:
        first = errs[0]
        raise ValidationFailed(f"no-code spec invalid at {first['path'] or '(root)'}: {first['message']}", errors=errs)
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    indicators = {
        iid: (ind["type"], ind.get("source", "close") if ind["type"] != "atr" else "close",
              int(ind["period"]), int(ind.get("shift", 0)))
        for iid, ind in sorted(spec.get("indicators", {}).items())
    }
    rules = [(_compile_when(r["when"]), float(r["weight"])) for r in spec["rules"]]
    max_lev = spec["max_leverage"]
    # All values below are validated primitives; repr() of str/int/float/tuple/list/dict is a literal.
    src = (
        f'"""Generated by the aijalon.trade no-code builder. spec sha256 {digest}. Do not edit by hand."""\n'
        f"MARKETS = {list(spec['markets'])!r}\n"
        f"TIMEFRAME = {spec['timeframe']!r}\n"
        f"LOOKBACK = {int(spec['lookback'])!r}\n"
        f"MAX_LEVERAGE = {max_lev!r}\n"
        f"INDICATORS = {indicators!r}\n"
        f"RULES = {rules!r}\n"
        f"DEFAULT_WEIGHT = {float(spec.get('default_weight', 0))!r}\n"
        + _INTERPRETER
    )
    ensure_valid(src, platform_max_leverage=platform_max_leverage)  # one execution path: same validator
    return src
