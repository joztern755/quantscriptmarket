"""RSI mean reversion on SOL (daily).

Enter long 1x when RSI(14) closes below 30; exit when RSI(14) closes above 55 or after 15 bars.
Scripts are stateless (every bar close runs a fresh module), so the current position is derived by
replaying the entry/exit rules over the supplied window. That makes backtest and live identical.
"""
import math

MARKETS = ["SOL"]
TIMEFRAME = "1d"
LOOKBACK = 120
MAX_LEVERAGE = 1

PERIOD = 14
ENTRY = 30.0
EXIT = 55.0
MAX_HOLD = 15


def rsi_series(closes, n):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag = gains / n
    al = losses / n
    for i in range(n, len(closes)):
        if i > n:
            d = closes[i] - closes[i - 1]
            ag = (ag * (n - 1) + max(d, 0.0)) / n
            al = (al * (n - 1) + max(-d, 0.0)) / n
        if al == 0:
            out[i] = 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


def signal(bars):
    closes = [b["c"] for b in bars["SOL"]]
    rsi = rsi_series(closes, PERIOD)
    in_pos = False
    held = 0
    for value in rsi:
        if value is None or math.isnan(value):
            continue
        if in_pos:
            held += 1
            if value > EXIT or held >= MAX_HOLD:
                in_pos = False
        elif value < ENTRY:
            in_pos = True
            held = 0
    return {"SOL": 1.0 if in_pos else 0.0}
