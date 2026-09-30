"""Donchian breakout on silver (Hyperliquid builder-deployed perp xyz:SILVER, daily).

Long when the close breaks above the highest high of the previous 20 bars, short when it breaks
below the lowest low of the previous 20 bars; exit a long below the 10-bar low and a short above
the 10-bar high. Volatility-scaled: target annualised volatility 30 %, capped at MAX_LEVERAGE.
State is replayed over the supplied window (scripts are stateless).
"""
import math
import statistics

MARKETS = ["xyz:SILVER"]
TIMEFRAME = "1d"
LOOKBACK = 120
MAX_LEVERAGE = 1.5

ENTRY_N = 20
EXIT_N = 10
VOL_N = 30
TARGET_VOL = 0.30


def signal(bars):
    rows = bars["xyz:SILVER"]
    highs = [b["h"] for b in rows]
    lows = [b["l"] for b in rows]
    closes = [b["c"] for b in rows]
    side = 0
    for i in range(ENTRY_N, len(rows)):
        c = closes[i]
        if side == 0:
            if c > max(highs[i - ENTRY_N:i]):
                side = 1
            elif c < min(lows[i - ENTRY_N:i]):
                side = -1
        elif side == 1 and c < min(lows[i - EXIT_N:i]):
            side = 0
        elif side == -1 and c > max(highs[i - EXIT_N:i]):
            side = 0
    if side == 0 or len(closes) < VOL_N + 1:
        return {"xyz:SILVER": 0.0}
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - VOL_N, len(closes))]
    vol = statistics.pstdev(rets) * math.sqrt(365)
    if vol <= 0:
        return {"xyz:SILVER": 0.0}
    size = min(MAX_LEVERAGE, TARGET_VOL / vol)
    return {"xyz:SILVER": side * round(size, 4)}
