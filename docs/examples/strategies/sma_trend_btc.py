"""SMA trend filter on BTC (daily).

Long 1x while the close is above the 200-day SMA and the 50-day SMA is above the 200-day SMA;
otherwise flat. Stateless: the decision depends only on the bars supplied for this bar close.
"""
MARKETS = ["BTC"]
TIMEFRAME = "1d"
LOOKBACK = 250
MAX_LEVERAGE = 1

FAST = 50
SLOW = 200


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def signal(bars):
    closes = [b["c"] for b in bars["BTC"]]
    fast = sma(closes, FAST)
    slow = sma(closes, SLOW)
    if fast is None or slow is None:
        return {"BTC": 0.0}
    if closes[-1] > slow and fast > slow:
        return {"BTC": 1.0}
    return {"BTC": 0.0}
