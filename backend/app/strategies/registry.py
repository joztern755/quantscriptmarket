"""In-house strategy registry (SPEC §7, §10). Pure data + helpers; no I/O.

Owner decision (30 Sep 2026): launch with SILVER only. The other CREST scripts are seeded as ``draft`` (not
visible) until their walk-forward passes. ``Settings.in_house_listed`` (env ``IN_HOUSE_LISTED``) decides which
keys are listed; it defaults to ``("silver",)``.

In-house weights come from the Ed25519-signed terminal feed ``signals.json``:
``strategies: {key: {target_weight: 0|1|2, last_action, last_action_date, market}}`` — long-only.
``terminal_weights_bps`` validates one feed entry against this registry (fail closed) and maps it to the
``signals`` table shape ``{coin: target_weight_bps}`` (weight × 10000).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.errors import ValidationFailed
from app.money import BPS

__all__ = [
    "InHouseStrategy",
    "IN_HOUSE",
    "SIGNAL_KEY_TO_MARKETS",
    "get",
    "by_slug",
    "status_for",
    "listed",
    "markets_for_signal_key",
    "market_for_signal_key",
    "terminal_weights_bps",
]

SOURCE_TERMINAL = "terminal"


@dataclass(frozen=True)
class InHouseStrategy:
    key: str                          # key in the terminal's signals.json
    slug: str
    name: str
    markets: tuple[str, ...]          # Hyperliquid perp coins ("dex:COIN" for builder-deployed markets)
    timeframe: str = "1d"
    source: str = SOURCE_TERMINAL
    long_only: bool = True
    allowed_weights: tuple[int, ...] = (0, 1, 2)
    max_leverage_x100: int = 200      # CREST: weight 2 = 2x
    in_house: bool = True
    notes: str = ""

    def max_weight(self) -> int:
        return max(self.allowed_weights)


IN_HOUSE: tuple[InHouseStrategy, ...] = (
    InHouseStrategy("silver", "crest-silver", "CREST Silver", ("xyz:SILVER",),
                    notes="Listed at launch: the only CREST script passing its walk-forward (REASSESSMENT.md, 28 Sep 2026)."),
    InHouseStrategy("btc", "crest-btc", "CREST Bitcoin", ("BTC",)),
    InHouseStrategy("sol", "crest-sol", "CREST Solana", ("SOL",)),
    InHouseStrategy("hype", "crest-hype", "CREST HYPE", ("HYPE",)),
    InHouseStrategy("gold", "crest-gold", "CREST Gold", ("xyz:GOLD",)),
    # [CONFIRM] WTI (xyz:CL) vs Brent (xyz:BRENTOIL) — SPEC §7.
    InHouseStrategy("oil", "crest-oil", "CREST Oil", ("xyz:CL",), notes="[CONFIRM] WTI vs Brent."),
    # Multi-market; per-coin members are not defined yet → cannot be mapped until they are (stays draft).
    InHouseStrategy("runners", "crest-runners", "CREST Runners", (), notes="Members TBD; multi-market."),
)

_BY_KEY = {s.key: s for s in IN_HOUSE}
_BY_SLUG = {s.slug: s for s in IN_HOUSE}

#: Terminal signal key → Hyperliquid markets (SPEC §7 "Market mapping").
SIGNAL_KEY_TO_MARKETS: Mapping[str, tuple[str, ...]] = {s.key: s.markets for s in IN_HOUSE}


def get(key: str) -> InHouseStrategy:
    try:
        return _BY_KEY[key.strip().lower()]
    except KeyError:
        raise ValidationFailed("unknown in-house strategy", key=key) from None


def by_slug(slug: str) -> InHouseStrategy:
    try:
        return _BY_SLUG[slug]
    except KeyError:
        raise ValidationFailed("unknown in-house strategy slug", slug=slug) from None


def status_for(key: str, listed_keys: Iterable[str] = ("silver",)) -> str:
    """``listed`` if the key is in ``Settings.in_house_listed`` (and mappable), else ``draft``."""
    s = get(key)
    return "listed" if s.key in {k.strip().lower() for k in listed_keys} and s.markets else "draft"


def listed(listed_keys: Iterable[str] = ("silver",)) -> tuple[InHouseStrategy, ...]:
    keys = {k.strip().lower() for k in listed_keys}
    return tuple(s for s in IN_HOUSE if s.key in keys and s.markets)


def markets_for_signal_key(key: str) -> tuple[str, ...]:
    s = get(key)
    if not s.markets:
        raise ValidationFailed("no market mapping for in-house strategy", key=key)
    return s.markets


def market_for_signal_key(key: str) -> str:
    """Single-market strategies only."""
    m = markets_for_signal_key(key)
    if len(m) != 1:
        raise ValidationFailed("multi-market strategy has no single market", key=key)
    return m[0]


def terminal_weights_bps(key: str, entry: Mapping[str, Any]) -> dict[str, int]:
    """Validate one ``signals.json`` strategy entry and return ``{coin: weight_bps}``.

    Fail closed: unknown key, non-integer or disallowed weight (CREST is long-only {0, 1, 2}), or a ``market``
    field that disagrees with the registry mapping → ``ValidationFailed``.
    """
    s = get(key)
    market = market_for_signal_key(key)
    w = entry.get("target_weight")
    if isinstance(w, float) and w.is_integer():  # JSON may encode 1 as 1.0
        w = int(w)
    if isinstance(w, bool) or not isinstance(w, int):
        raise ValidationFailed("target_weight must be an integer", key=key, target_weight=w)
    if w not in s.allowed_weights:
        raise ValidationFailed("target_weight not allowed", key=key, target_weight=w, allowed=s.allowed_weights)
    if s.long_only and w < 0:
        raise ValidationFailed("long-only strategy got a short weight", key=key)
    feed_market = entry.get("market")
    if feed_market is not None and feed_market != market:
        raise ValidationFailed("signal market does not match registry", key=key, feed_market=feed_market,
                               expected=market)
    return {market: w * BPS}
