"""Fill and funding attribution to subscriptions (SPEC §1.1). Pure functions over info-endpoint payloads.

Field semantics VERIFIED on mainnet fills (2026-09-30, 2000+ fills of an agent-traded account; fixtures
``userFills_sample.json`` / ``userFunding_sample.json``):
- Fill keys: coin, px, sz, side ("B"/"A"), time, startPosition, dir, closedPnl, hash, oid, crossed, fee, tid,
  feeToken, twapId; OPTIONAL: ``builderFee`` (only when the order carried a builder code), ``cloid`` (only when
  the order had one; every partial fill of an order repeats it), ``liquidation``.
- ``fee`` INCLUDES ``builderFee``: same account, same market — without builder 4.32 bp, with builder 14.32 bp;
  builderFee is exactly 10.00 bp (f=100) of notional; the remainder equals the no-builder rate (also on xyz).
- ``closedPnl`` is GROSS of fees: it matched (exit − avg entry) × closed size on 1348/1354 reconstructed closes
  (the rest differ by ≤ 0.011 from our avg-entry rounding).
- ⇒ net PnL of a fill = ``closedPnl − fee``. builderFee must NOT be subtracted again.
- Fills sharing one timestamp are NOT ordered by ``tid``; the ``startPosition`` chain gives the true order. Our
  position timeline therefore aggregates per millisecond.
- Funding: ``delta.usdc`` > 0 = paid TO the user (272/272 entries have sign(usdc) = −sign(szi × fundingRate));
  ``szi`` is the account's signed position at that funding; hash is all zeros (idempotency key must be
  (trading_address, coin, time), as SPEC §4 says). Funding times sit on the hour ± a few ms.

Rounding (documented contract): all Hyperliquid perp USDC amounts observed have ≤ 6 decimals, so conversion to
micro is exact. Where it is not (funding pro-rata), values are FLOORED toward −∞: attributed PnL is never
overstated, so profit share is never overcharged. ``net_pnl_micro = floor((closedPnl − fee) × 1e6)`` is computed
on the exact Decimal difference (one rounding, not two).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Iterable, Mapping, Sequence

from app.errors import ValidationFailed
from app.hl.client import is_platform_cloid
from app.money import parse_decimal, to_micro

__all__ = [
    "Fill", "FundingEvent", "SubscriptionWindow", "AttributedFill", "FillAttribution", "AttributedFunding",
    "FundingAttribution", "parse_fill", "parse_funding", "is_perp_coin", "attribute_fills", "position_timeline",
    "position_before", "attribute_funding", "pnl_summary",
]

ZERO = Decimal(0)


def is_perp_coin(coin: str) -> bool:
    """Spot fills use "@123" or "PURR/USDC"; perps use "BTC" or "dex:COIN"."""
    return bool(coin) and not coin.startswith("@") and "/" not in coin


@dataclass(frozen=True)
class Fill:
    coin: str
    px: Decimal
    sz: Decimal
    side: str                 # "B" buy | "A" sell
    time_ms: int
    start_position: Decimal
    dir: str
    closed_pnl: Decimal
    fee: Decimal              # total fee INCLUDING builder fee
    fee_token: str
    builder_fee: Decimal      # 0 when the key is absent
    tid: int
    oid: int
    cloid: str | None
    hash: str
    crossed: bool
    is_liquidation: bool
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def signed_sz(self) -> Decimal:
        return self.sz if self.side == "B" else -self.sz

    @property
    def is_perp(self) -> bool:
        return is_perp_coin(self.coin)

    @property
    def notional(self) -> Decimal:
        return self.px * self.sz

    @property
    def net_pnl_micro(self) -> int:
        return to_micro(self.closed_pnl - self.fee, ROUND_FLOOR)

    @property
    def closed_pnl_micro(self) -> int:
        return to_micro(self.closed_pnl, ROUND_FLOOR)

    @property
    def fee_micro(self) -> int:
        return to_micro(self.fee, ROUND_FLOOR)

    @property
    def builder_fee_micro(self) -> int:
        """Revenue recognised from this fill; floored (never recognise more than Hyperliquid reported)."""
        return to_micro(self.builder_fee, ROUND_FLOOR)


def parse_fill(raw: Mapping[str, Any]) -> Fill:
    try:
        side = raw["side"]
        if side not in ("B", "A"):
            raise ValueError(f"bad side {side!r}")
        cloid = raw.get("cloid")
        return Fill(
            coin=str(raw["coin"]), px=parse_decimal(raw["px"]), sz=parse_decimal(raw["sz"]), side=side,
            time_ms=int(raw["time"]), start_position=parse_decimal(raw.get("startPosition", "0")),
            dir=str(raw.get("dir", "")), closed_pnl=parse_decimal(raw["closedPnl"]), fee=parse_decimal(raw["fee"]),
            fee_token=str(raw.get("feeToken", "")), builder_fee=parse_decimal(raw.get("builderFee", "0")),
            tid=int(raw["tid"]), oid=int(raw["oid"]), cloid=cloid.lower() if isinstance(cloid, str) else None,
            hash=str(raw.get("hash", "")), crossed=bool(raw.get("crossed", False)),
            is_liquidation=raw.get("liquidation") is not None, raw=raw,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ValidationFailed("unparseable fill", error=str(e)) from e


@dataclass(frozen=True)
class SubscriptionWindow:
    """Fallback attribution when a platform-prefixed fill's cloid is missing from the orders table (crash between
    placing and recording). SPEC §4 allows one active subscription per trading address."""
    subscription_id: str
    trading_address: str
    coins: frozenset[str]
    start_ms: int
    end_ms: int | None = None     # exclusive; None = still active

    def covers(self, trading_address: str, coin: str, t_ms: int) -> bool:
        return (trading_address.lower() == self.trading_address.lower() and coin in self.coins
                and t_ms >= self.start_ms and (self.end_ms is None or t_ms < self.end_ms))


@dataclass(frozen=True)
class AttributedFill:
    subscription_id: str
    trading_address: str
    fill: Fill
    via: str                      # "cloid" | "window"

    @property
    def net_pnl_micro(self) -> int:
        return self.fill.net_pnl_micro


@dataclass
class FillAttribution:
    attributed: list[AttributedFill] = field(default_factory=list)
    ours_unmatched: list[Fill] = field(default_factory=list)   # our prefix, no subscription found → ALERT
    foreign: list[Fill] = field(default_factory=list)          # user's own trades / other apps: ignored
    rejected: list[tuple[Mapping[str, Any], str]] = field(default_factory=list)  # unparseable / non-USDC / spot

    def by_subscription(self) -> dict[str, list[AttributedFill]]:
        out: dict[str, list[AttributedFill]] = {}
        for a in self.attributed:
            out.setdefault(a.subscription_id, []).append(a)
        return out


def attribute_fills(raw_fills: Iterable[Mapping[str, Any]], *, trading_address: str,
                    cloid_to_subscription: Mapping[str, str],
                    windows: Sequence[SubscriptionWindow] = ()) -> FillAttribution:
    """Attribute one trading address's fills. Only perp fills in USDC carrying OUR cloid prefix count; the
    orders table (cloid → subscription) is authoritative, ``windows`` is the crash fallback. Duplicate tids
    (overlapping pages) are dropped."""
    out = FillAttribution()
    seen: set[int] = set()
    cmap = {k.lower(): v for k, v in cloid_to_subscription.items()}
    addr = trading_address.lower()
    for raw in raw_fills:
        try:
            f = parse_fill(raw)
        except ValidationFailed as e:
            out.rejected.append((raw, f"unparseable: {e.details.get('error', '')}"))
            continue
        if f.tid in seen:
            continue
        seen.add(f.tid)
        if not is_platform_cloid(f.cloid):
            out.foreign.append(f)
            continue
        if not f.is_perp:
            out.rejected.append((raw, "platform cloid on a spot fill"))
            continue
        if f.fee_token != "USDC":
            out.rejected.append((raw, f"fee token {f.fee_token!r} not USDC"))
            continue
        sub = cmap.get(f.cloid or "")
        via = "cloid"
        if sub is None:
            hits = [w for w in windows if w.covers(addr, f.coin, f.time_ms)]
            if len(hits) == 1:
                sub, via = hits[0].subscription_id, "window"
        if sub is None:
            out.ours_unmatched.append(f)
            continue
        out.attributed.append(AttributedFill(sub, addr, f, via))
    out.attributed.sort(key=lambda a: (a.fill.time_ms, a.fill.tid))
    return out


def position_timeline(fills: Iterable[Fill], coin: str, start_position: Decimal = ZERO) -> list[tuple[int, Decimal]]:
    """[(time_ms, position AFTER all of this subscription's fills at that ms)], ascending, one row per ms."""
    per_ms: dict[int, Decimal] = {}
    for f in fills:
        if f.coin == coin:
            per_ms[f.time_ms] = per_ms.get(f.time_ms, ZERO) + f.signed_sz
    pos, out = start_position, []
    for t in sorted(per_ms):
        pos += per_ms[t]
        out.append((t, pos))
    return out


def position_before(timeline: Sequence[tuple[int, Decimal]], t_ms: int, start_position: Decimal = ZERO) -> Decimal:
    """Subscription position held just before ``t_ms`` (fills at exactly ``t_ms`` are not yet included)."""
    pos = start_position
    for t, p in timeline:
        if t >= t_ms:
            break
        pos = p
    return pos


@dataclass(frozen=True)
class FundingEvent:
    time_ms: int
    coin: str
    usdc: Decimal             # + received / − paid by the ACCOUNT
    szi: Decimal              # account's signed position at the funding
    funding_rate: Decimal
    hash: str

    @staticmethod
    def parse(raw: Mapping[str, Any]) -> "FundingEvent":
        return parse_funding(raw)


def parse_funding(raw: Mapping[str, Any]) -> FundingEvent:
    try:
        d = raw["delta"]
        if d.get("type") != "funding":
            raise ValueError("not a funding delta")
        return FundingEvent(time_ms=int(raw["time"]), coin=str(d["coin"]), usdc=parse_decimal(d["usdc"]),
                            szi=parse_decimal(d["szi"]), funding_rate=parse_decimal(d["fundingRate"]),
                            hash=str(raw.get("hash", "")))
    except (KeyError, TypeError, ValueError) as e:
        raise ValidationFailed("unparseable funding", error=str(e)) from e


@dataclass(frozen=True)
class AttributedFunding:
    subscription_id: str
    coin: str
    time_ms: int
    usdc_micro: int           # attributed amount (floored toward −∞)
    share: Decimal            # our position / account position (0..1)
    event: FundingEvent


@dataclass
class FundingAttribution:
    attributed: list[AttributedFunding] = field(default_factory=list)
    anomalies: list[tuple[FundingEvent, str]] = field(default_factory=list)  # e.g. account flat/opposite → alert

    @property
    def total_micro(self) -> int:
        return sum(a.usdc_micro for a in self.attributed)


def attribute_funding(raw_funding: Iterable[Mapping[str, Any]], *, subscription_id: str,
                      fills: Iterable[Fill], coins: Iterable[str], start_ms: int, end_ms: int | None = None,
                      start_positions: Mapping[str, Decimal] | None = None) -> FundingAttribution:
    """Funding on the strategy's coins while the SUBSCRIPTION held a position (per its own fills).

    share = our_position / account_szi, clamped to [0, 1]: if the user also trades the same coin by hand only our
    part of the funding is attributed; if our position exceeds the account's (user reduced it manually) all of the
    account's funding is attributed. Opposite signs or a flat account while we think we hold a position are
    anomalies (share 0, reported)."""
    coins = set(coins)
    fills = [f for f in fills if f.coin in coins]
    starts = dict(start_positions or {})
    timelines = {c: position_timeline(fills, c, starts.get(c, ZERO)) for c in coins}
    out = FundingAttribution()
    seen: set[tuple[int, str]] = set()
    for raw in raw_funding:
        ev = parse_funding(raw)
        if ev.coin not in coins or ev.time_ms < start_ms or (end_ms is not None and ev.time_ms >= end_ms):
            continue
        if (ev.time_ms, ev.coin) in seen:
            continue
        seen.add((ev.time_ms, ev.coin))
        ours = position_before(timelines[ev.coin], ev.time_ms, starts.get(ev.coin, ZERO))
        if ours == 0:
            continue
        if ev.szi == 0 or (ours > 0) != (ev.szi > 0):
            out.anomalies.append((ev, "account position flat or opposite to subscription position"))
            continue
        share = min(Decimal(1), abs(ours) / abs(ev.szi))
        out.attributed.append(AttributedFunding(subscription_id, ev.coin, ev.time_ms,
                                                to_micro(ev.usdc * share, ROUND_FLOOR), share, ev))
    out.attributed.sort(key=lambda a: (a.time_ms, a.coin))
    return out


def pnl_summary(attributed: Iterable[AttributedFill], funding: FundingAttribution | None = None) -> dict[str, int]:
    """Σ for the settlement port's ``PnlDelta``: realized = Σ net_pnl_micro; funding = Σ attributed funding."""
    fills = list(attributed)
    return {
        "realized_micro": sum(a.fill.net_pnl_micro for a in fills),
        "fees_micro": sum(a.fill.fee_micro for a in fills),
        "builder_fees_micro": sum(a.fill.builder_fee_micro for a in fills),
        "funding_micro": funding.total_micro if funding else 0,
    }
