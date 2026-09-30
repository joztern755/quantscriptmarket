"""Market catalog: coin → asset id, size/price grid, leverage and margin-mode flags, live context (SPEC §6).

Asset ids
- Validator perps (dex ""): ``asset = index in meta.universe`` (BTC = 0).
- Builder-deployed (HIP-3) perps: ``asset = 100000 + dex_index * 10000 + index in that dex's meta.universe`` where
  ``dex_index`` is the dex's POSITION IN THE ``perpDexs`` LIST (position 0 is ``null`` = the validator dex).
  Evidence (2026-09-30 mainnet): perpDexs = [null, xyz, flx, vntl, hyna, km, abcd, cash, para, mkts, io] so
  ``xyz`` is 1; ``meta(dex="xyz")`` lists ``xyz:SILVER`` at index 26 → asset 110026. The official SDK derives
  the same numbers (``110000 + i*10000`` over ``perpDexs[1:]``, i.e. 100000 + position*10000) — recalled from
  its source, which is not reachable from this environment. The on-chain order action could not be inspected
  here (explorer blocked): VERIFY with one testnet/mainnet order before go-live.
- Builder-dex universe names already carry the prefix (``xyz:SILVER``); we refuse metas that do not.

Grid (Hyperliquid tick/lot rules)
- Size: rounded DOWN to ``szDecimals``.
- Price (perps): ≤ 5 significant figures and ≤ ``6 − szDecimals`` decimals; integer prices are always valid.
  Strings are canonical: no exponent, no trailing zeros. Rounding direction is always explicit: an IOC buy
  rounds DOWN and a sell rounds UP (toward the mid) so the slippage cap is never exceeded.

Margin modes seen in meta (VERIFIED 2026-09-30)
- ``onlyIsolated: true`` with ``marginMode: "noCross"`` (live HIP-3 equities on xyz, e.g. xyz:HOOD) or
  ``"strictIsolated"`` (seen only on delisted markets). Launch markets BTC, SOL, HYPE, xyz:GOLD, xyz:SILVER,
  xyz:CL, xyz:BRENTOIL carry neither flag → cross margin allowed (observed: live cross positions on xyz).
- ``marginTableId`` < 50 on builder dexes is not listed in ``marginTables`` (implicit single tier = maxLeverage).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any, Iterable, Mapping, Sequence

from app.domain.risk import MarketSnapshot
from app.errors import ExternalServiceError, GuardRejected, NotFound, ValidationFailed
from app.money import BPS, parse_decimal, to_micro

__all__ = [
    "PERP_MAX_DECIMALS", "MAX_SIG_FIGS", "BUILDER_DEX_BASE", "BUILDER_DEX_STRIDE",
    "Market", "AssetCtx", "MarketCatalog",
    "asset_id_for", "canonical", "format_sz", "format_px", "is_valid_px", "is_valid_sz", "ioc_limit_px",
]

PERP_MAX_DECIMALS = 6
MAX_SIG_FIGS = 5
BUILDER_DEX_BASE = 100_000
BUILDER_DEX_STRIDE = 10_000


def asset_id_for(dex_index: int, index: int) -> int:
    if dex_index < 0 or index < 0:
        raise ValidationFailed("negative asset index")
    if dex_index == 0:
        return index
    if index >= BUILDER_DEX_STRIDE:
        raise ValidationFailed("builder-dex index out of range")
    return BUILDER_DEX_BASE + dex_index * BUILDER_DEX_STRIDE + index


def canonical(d: Decimal) -> str:
    """Decimal → Hyperliquid wire string: no exponent, no trailing zeros ("0.0100" → "0.01", "1E+2" → "100")."""
    if not d.is_finite():
        raise ValidationFailed("non-finite number")
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")


def _dec(v: Decimal | str | int) -> Decimal:
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise ValidationFailed("non-finite number")
        return v
    try:
        return parse_decimal(v)
    except (TypeError, ValueError) as e:
        raise ValidationFailed(str(e)) from e


def format_sz(sz: Decimal | str | int, sz_decimals: int) -> str:
    """Round a positive size DOWN to the lot size. Returns "0" when it rounds to nothing (caller must skip)."""
    d = _dec(sz)
    if d < 0:
        raise ValidationFailed("size must be positive; side carries the sign")
    return canonical(d.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN))


def _px_decimals(px: Decimal, sz_decimals: int) -> int:
    return max(0, min(PERP_MAX_DECIMALS - sz_decimals, MAX_SIG_FIGS - 1 - px.adjusted()))


def format_px(px: Decimal | str | int, sz_decimals: int, *, is_buy: bool | None = None,
              rounding: str | None = None) -> str:
    """Round a perp price onto the grid. Pass ``is_buy`` (buy → floor, sell → ceiling: toward the mid) or an
    explicit ``rounding``; exactly one is required."""
    if (is_buy is None) == (rounding is None):
        raise ValidationFailed("pass exactly one of is_buy / rounding")
    mode = rounding if rounding is not None else (ROUND_FLOOR if is_buy else ROUND_CEILING)
    d = _dec(px)
    if d <= 0:
        raise ValidationFailed("price must be positive")
    decimals = _px_decimals(d, sz_decimals)
    out = d.quantize(Decimal(1).scaleb(-decimals), rounding=mode)
    decimals2 = _px_decimals(out, sz_decimals) if out > 0 else decimals
    if decimals2 < decimals:  # rounding up added a digit (9.99995 → 10.0000): re-apply the sig-fig limit
        out = out.quantize(Decimal(1).scaleb(-decimals2), rounding=mode)
    if out <= 0:
        raise ValidationFailed("price rounds to zero")
    return canonical(out)


def is_valid_px(px: str | Decimal, sz_decimals: int) -> bool:
    try:
        d = _dec(px)
    except ValidationFailed:
        return False
    if d <= 0:
        return False
    if d == d.to_integral_value():
        return True
    exp = -d.normalize().as_tuple().exponent  # decimals actually used
    sig = len(d.normalize().as_tuple().digits)
    return exp <= PERP_MAX_DECIMALS - sz_decimals and sig <= MAX_SIG_FIGS


def is_valid_sz(sz: str | Decimal, sz_decimals: int) -> bool:
    try:
        d = _dec(sz)
    except ValidationFailed:
        return False
    return d > 0 and d == d.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN)


def ioc_limit_px(mid: Decimal | str, sz_decimals: int, *, is_buy: bool, slippage_bps: int) -> str:
    """IOC limit = mid × (1 ± slippage), rounded toward the mid (never beyond the cap)."""
    if slippage_bps < 0 or slippage_bps >= BPS:
        raise ValidationFailed("bad slippage")
    m = _dec(mid)
    factor = (Decimal(BPS + slippage_bps) if is_buy else Decimal(BPS - slippage_bps)) / BPS
    return format_px(m * factor, sz_decimals, is_buy=is_buy)


@dataclass(frozen=True)
class Market:
    coin: str                   # "BTC" | "xyz:SILVER"
    dex: str                    # "" for validator perps
    dex_index: int              # position in perpDexs (0 = validator dex)
    index: int                  # position in that dex's meta.universe
    asset_id: int
    sz_decimals: int
    max_leverage: int
    is_delisted: bool
    only_isolated: bool
    margin_mode: str | None     # None | "noCross" | "strictIsolated" (as reported)
    margin_table_id: int | None

    @property
    def allows_cross(self) -> bool:
        return not self.only_isolated and self.margin_mode not in ("noCross", "strictIsolated")

    @property
    def px_max_decimals(self) -> int:
        return PERP_MAX_DECIMALS - self.sz_decimals

    def format_sz(self, sz: Decimal | str | int) -> str:
        return format_sz(sz, self.sz_decimals)

    def format_px(self, px: Decimal | str | int, *, is_buy: bool | None = None, rounding: str | None = None) -> str:
        return format_px(px, self.sz_decimals, is_buy=is_buy, rounding=rounding)


@dataclass(frozen=True)
class AssetCtx:
    mark_px: Decimal
    oracle_px: Decimal
    mid_px: Decimal | None      # null when one side of the book is empty (e.g. delisted markets)
    day_ntl_vlm: Decimal        # USD
    open_interest: Decimal      # COIN units
    funding: Decimal
    premium: Decimal | None
    prev_day_px: Decimal | None
    impact_pxs: tuple[Decimal, Decimal] | None

    @property
    def open_interest_usd(self) -> Decimal:
        return self.open_interest * self.mark_px

    @staticmethod
    def parse(raw: Mapping[str, Any]) -> "AssetCtx":
        def opt(k: str) -> Decimal | None:
            v = raw.get(k)
            return None if v is None else parse_decimal(v)

        try:
            imp = raw.get("impactPxs")
            return AssetCtx(
                mark_px=parse_decimal(raw["markPx"]), oracle_px=parse_decimal(raw["oraclePx"]), mid_px=opt("midPx"),
                day_ntl_vlm=parse_decimal(raw["dayNtlVlm"]), open_interest=parse_decimal(raw["openInterest"]),
                funding=parse_decimal(raw["funding"]), premium=opt("premium"), prev_day_px=opt("prevDayPx"),
                impact_pxs=(parse_decimal(imp[0]), parse_decimal(imp[1])) if imp else None,
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ExternalServiceError("bad asset ctx", error=str(e)) from e


@dataclass
class MarketCatalog:
    markets: dict[str, Market]
    ctxs: dict[str, AssetCtx] = field(default_factory=dict)
    data_time: datetime | None = None                     # when the ctxs were fetched (tz-aware UTC)
    dex_names: tuple[str, ...] = ("",)                    # position = dex_index
    raw_meta: dict[str, dict] = field(default_factory=dict)  # dex → meta as received (SDK wiring needs "")

    # ---------------------------------------------------------------------------------------------- building

    @staticmethod
    def dex_index_map(perp_dexs: Sequence[Mapping[str, Any] | None]) -> dict[str, int]:
        if not perp_dexs or perp_dexs[0] is not None:
            raise ExternalServiceError("perpDexs[0] must be null (validator dex); asset-id formula would be wrong")
        out = {"": 0}
        for i, d in enumerate(perp_dexs[1:], start=1):
            name = (d or {}).get("name")
            if not isinstance(name, str) or not name or name in out:
                raise ExternalServiceError("bad perpDexs entry", position=i)
            out[name] = i
        return out

    @classmethod
    def build(cls, perp_dexs: Sequence[Mapping[str, Any] | None], metas: Mapping[str, Mapping[str, Any]],
              ctxs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
              data_time: datetime | None = None) -> "MarketCatalog":
        """``metas``/``ctxs`` keyed by dex name ("" = validator perps). ``ctxs`` lists align with the universe."""
        idx = cls.dex_index_map(perp_dexs)
        names = tuple(sorted(idx, key=idx.__getitem__))
        markets: dict[str, Market] = {}
        parsed: dict[str, AssetCtx] = {}
        for dex, meta in metas.items():
            if dex not in idx:
                raise ExternalServiceError("meta for unknown dex", dex=dex)
            universe = meta.get("universe")
            if not isinstance(universe, list):
                raise ExternalServiceError("meta without universe", dex=dex)
            dex_ctx = (ctxs or {}).get(dex)
            if dex_ctx is not None and len(dex_ctx) != len(universe):
                raise ExternalServiceError("ctx/universe length mismatch", dex=dex)
            for i, u in enumerate(universe):
                name = u.get("name")
                if not isinstance(name, str) or not name:
                    raise ExternalServiceError("universe entry without name", dex=dex, index=i)
                if dex and not name.startswith(dex + ":"):
                    raise ExternalServiceError("builder-dex coin without dex prefix", dex=dex, coin=name)
                if not dex and ":" in name:
                    raise ExternalServiceError("validator coin with dex prefix", coin=name)
                m = Market(
                    coin=name, dex=dex, dex_index=idx[dex], index=i, asset_id=asset_id_for(idx[dex], i),
                    sz_decimals=int(u["szDecimals"]), max_leverage=int(u["maxLeverage"]),
                    is_delisted=bool(u.get("isDelisted", False)), only_isolated=bool(u.get("onlyIsolated", False)),
                    margin_mode=u.get("marginMode"), margin_table_id=u.get("marginTableId"),
                )
                prev = markets.get(name)
                if prev is not None and not (prev.is_delisted and not m.is_delisted):
                    if not m.is_delisted:
                        raise ExternalServiceError("duplicate live coin name", coin=name)
                    continue  # keep the live (or first) listing
                markets[name] = m
                if dex_ctx is not None:
                    parsed[name] = AssetCtx.parse(dex_ctx[i])
        return cls(markets=markets, ctxs=parsed, data_time=data_time, dex_names=names,
                   raw_meta={k: dict(v) for k, v in metas.items()})

    @classmethod
    def from_info(cls, info: Any, dexes: Iterable[str] | None = None, *,
                  now: datetime | None = None) -> "MarketCatalog":
        """Fetch perpDexs + metaAndAssetCtxs for ``dexes`` (default: validator dex only). 1 + len(dexes) calls."""
        perp_dexs = info.perp_dexs()
        wanted = list(dict.fromkeys([""] + list(dexes or [])))
        metas: dict[str, dict] = {}
        ctxs: dict[str, list] = {}
        for dex in wanted:
            meta, ctx = info.meta_and_asset_ctxs(dex)
            metas[dex], ctxs[dex] = meta, ctx
        return cls.build(perp_dexs, metas, ctxs, now or datetime.now(timezone.utc))

    @staticmethod
    def dexes_for(coins: Iterable[str]) -> list[str]:
        return sorted({c.split(":", 1)[0] for c in coins if ":" in c})

    # ----------------------------------------------------------------------------------------------- lookups

    def market(self, coin: str) -> Market:
        m = self.markets.get(coin)
        if m is None:
            raise NotFound("unknown market", coin=coin)
        return m

    def __contains__(self, coin: object) -> bool:
        return coin in self.markets

    def asset_id(self, coin: str) -> int:
        return self.market(coin).asset_id

    def coin_for_asset(self, asset_id: int) -> str:
        for m in self.markets.values():
            if m.asset_id == asset_id:
                return m.coin
        raise NotFound("unknown asset id", asset_id=asset_id)

    def format_sz(self, coin: str, sz: Decimal | str | int) -> str:
        return self.market(coin).format_sz(sz)

    def format_px(self, coin: str, px: Decimal | str | int, *, is_buy: bool | None = None,
                  rounding: str | None = None) -> str:
        return self.market(coin).format_px(px, is_buy=is_buy, rounding=rounding)

    def ioc_limit_px(self, coin: str, *, is_buy: bool, slippage_bps: int, mid: Decimal | None = None) -> str:
        m = self.market(coin)
        if mid is None:
            mid = self.ctx(coin).mid_px
            if mid is None:
                raise GuardRejected("no mid price", coin=coin, reasons=("no_mid",))
        return ioc_limit_px(mid, m.sz_decimals, is_buy=is_buy, slippage_bps=slippage_bps)

    def ctx(self, coin: str) -> AssetCtx:
        self.market(coin)
        c = self.ctxs.get(coin)
        if c is None:
            raise NotFound("no live context for market", coin=coin)
        return c

    def to_snapshot(self, coin: str) -> MarketSnapshot:
        """``app.domain.risk.MarketSnapshot`` for the guards. USD amounts floored to the micro (smaller liquidity
        caps = safer). Fails closed (GuardRejected) when the book has no mid."""
        m, c = self.market(coin), self.ctx(coin)
        if c.mid_px is None:
            raise GuardRejected("no mid price", coin=coin, reasons=("no_mid",))
        if self.data_time is None or self.data_time.tzinfo is None:
            raise ValidationFailed("catalog data_time must be tz-aware")
        return MarketSnapshot(
            coin=coin, mid_px=c.mid_px, mark_px=c.mark_px, oracle_px=c.oracle_px,
            day_ntl_vlm_micro=to_micro(c.day_ntl_vlm, ROUND_FLOOR),
            open_interest_micro=to_micro(c.open_interest_usd, ROUND_FLOOR),
            max_leverage=m.max_leverage, sz_decimals=m.sz_decimals, is_delisted=m.is_delisted,
            data_time=self.data_time,
        )
