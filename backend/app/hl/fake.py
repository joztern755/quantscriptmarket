"""In-memory Hyperliquid for tests (executor, fills attribution, deposits, confirm endpoints). No network.

``FakeHyperliquid`` holds shared state; ``FakeExchangeGateway`` (ExchangeGateway / executor port) mutates it;
``FakeInfo`` (same methods as ``InfoClient``) reads it and falls back to the recorded mainnet fixtures in
``tests/fixtures/hl`` for market data. Fills are emitted in the exact mainnet shape (fee INCLUDES builderFee,
closedPnl gross of fees, cloid repeated on every partial fill) so ``app.hl.fills`` can consume them.

Scripting outcomes (FIFO, optionally per coin)::

    hl.script("partial", ratio=Decimal("0.4"))        # next order fills 40 %
    hl.script("reject", error="Insufficient margin")  # exchange-level order error
    hl.script("no_match")                             # IOC could not match
    hl.script("raise")                                # transport error, order NOT placed
    hl.script("raise_after_fill")                     # transport error, order WAS executed (unknown outcome)
    hl.script("err_status", error="Builder fee has not been approved.")
    hl.script("resting")
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterable

from app.errors import ExternalServiceError, NotFound, ValidationFailed
from app.hl.client import BuilderCode, LeverageResult, OrderResult, is_platform_cloid, normalize_order_response
from app.hl.info import normalize_address
from app.hl.markets import MarketCatalog, canonical, is_valid_px, is_valid_sz

__all__ = ["FIXTURES_DIR", "load_fixture", "FakeHyperliquid", "FakeExchangeGateway", "FakeGatewayFactory",
           "FakeInfo", "FakeTransportError", "seed_approvals"]

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "hl"
_Q6 = Decimal("0.000001")


def load_fixture(name: str) -> Any:
    with open(FIXTURES_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


class FakeTransportError(ConnectionError):
    """Raised by scripted "raise" outcomes (the executor must treat the outcome as unknown)."""


@dataclass
class _Behavior:
    kind: str
    coin: str | None = None
    ratio: Decimal | None = None
    error: str | None = None
    px: Decimal | None = None


@dataclass
class _Position:
    szi: Decimal = Decimal(0)
    entry_px: Decimal = Decimal(0)


@dataclass
class FakeAccount:
    address: str
    positions: dict[str, _Position] = field(default_factory=dict)
    fills: list[dict] = field(default_factory=list)
    orders: dict[str, dict] = field(default_factory=dict)       # cloid → orderStatus "order" wrapper
    leverage: dict[str, tuple[int, bool]] = field(default_factory=dict)
    funding: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)


def _q6(d: Decimal) -> Decimal:
    return d.quantize(_Q6, rounding=ROUND_HALF_UP)


def _s(d: Decimal) -> str:
    return canonical(d) if d != d.to_integral_value() else canonical(d) + ".0"


class FakeHyperliquid:
    def __init__(self, *, catalog: MarketCatalog | None = None, taker_fee_rate: Decimal = Decimal("0.00045"),
                 now_ms: int = 1_790_000_000_000) -> None:
        self.catalog = catalog
        self.taker_fee_rate = taker_fee_rate
        self.now_ms = now_ms
        self.mids: dict[str, Decimal] = {}
        self.accounts: dict[str, FakeAccount] = {}
        self.orders_log: list[dict] = []           # every order the gateway sent, as the wire would see it
        self.leverage_log: list[dict] = []
        self.agents: dict[str, list[dict]] = {}
        self.roles: dict[str, dict] = {}
        self.builder_fees: dict[tuple[str, str], int] = {}
        self._behaviors: deque[_Behavior] = deque()
        self._next_oid = 500_000_000_000
        self._next_tid = 900_000_000_000_000

    # ------------------------------------------------------------------------------------------- scripting

    def script(self, kind: str, *, coin: str | None = None, ratio: Decimal | str | None = None,
               error: str | None = None, px: Decimal | str | None = None) -> None:
        if kind not in ("fill", "partial", "reject", "no_match", "raise", "raise_after_fill", "err_status",
                        "resting"):
            raise ValueError(kind)
        self._behaviors.append(_Behavior(kind, coin, Decimal(str(ratio)) if ratio is not None else None, error,
                                         Decimal(str(px)) if px is not None else None))

    def _take_behavior(self, coin: str) -> _Behavior:
        for i, b in enumerate(self._behaviors):
            if b.coin is None or b.coin == coin:
                del self._behaviors[i]
                return b
        return _Behavior("fill")

    def set_mid(self, coin: str, px: Decimal | str) -> None:
        self.mids[coin] = Decimal(str(px))

    def account(self, address: str) -> FakeAccount:
        a = address.lower()
        if a not in self.accounts:
            self.accounts[a] = FakeAccount(a)
        return self.accounts[a]

    def position(self, address: str, coin: str) -> Decimal:
        return self.account(address).positions.get(coin, _Position()).szi

    def tick(self, ms: int = 1) -> int:
        self.now_ms += ms
        return self.now_ms

    # ------------------------------------------------------------------------------------------ mechanics

    def _dir(self, start: Decimal, end: Decimal) -> str:
        if start >= 0 and end > start:
            return "Open Long"
        if start <= 0 and end < start:
            return "Open Short"
        if start > 0 and end < 0:
            return "Long > Short"
        if start < 0 and end > 0:
            return "Short > Long"
        return "Close Long" if start > 0 else "Close Short"

    def apply_fill(self, address: str, coin: str, is_buy: bool, sz: Decimal, px: Decimal, *, cloid: str | None,
                   oid: int, builder: BuilderCode | None) -> dict:
        acct = self.account(address)
        pos = acct.positions.setdefault(coin, _Position())
        start = pos.szi
        signed = sz if is_buy else -sz
        end = start + signed
        closing = min(sz, abs(start)) if start != 0 and (start > 0) != is_buy else Decimal(0)
        closed_pnl = (px - pos.entry_px) * closing * (1 if start > 0 else -1) if closing else Decimal(0)
        opening = sz - closing
        if end == 0:
            pos.entry_px = Decimal(0)
        elif opening > 0 and closing == 0:
            pos.entry_px = (abs(start) * pos.entry_px + opening * px) / abs(end)
        elif opening > 0:  # flip
            pos.entry_px = px
        pos.szi = end
        notional = px * sz
        builder_fee = _q6(notional * builder.fee_tenths_bp / Decimal(100_000)) if builder else Decimal(0)
        fee = _q6(notional * self.taker_fee_rate) + builder_fee
        self._next_tid += 7
        fill = {
            "coin": coin, "px": _s(px), "sz": canonical(sz), "side": "B" if is_buy else "A", "time": self.now_ms,
            "startPosition": _s(start), "dir": self._dir(start, end), "closedPnl": _s(_q6(closed_pnl)),
            "hash": "0x" + f"{oid:064x}", "oid": oid, "crossed": True, "fee": _s(fee), "tid": self._next_tid,
            "feeToken": "USDC", "twapId": None,
        }
        if builder:
            fill["builderFee"] = _s(builder_fee)
        if cloid:
            fill["cloid"] = cloid
        acct.fills.append(fill)
        return fill

    def add_funding(self, address: str, coin: str, rate: Decimal | str, *, time_ms: int | None = None) -> dict | None:
        """Hourly funding on the account's current position: usdc = −szi × mid × rate (VERIFIED sign)."""
        acct = self.account(address)
        szi = acct.positions.get(coin, _Position()).szi
        if szi == 0:
            return None
        rate = Decimal(str(rate))
        mid = self.mids.get(coin) or acct.positions[coin].entry_px
        ev = {"time": time_ms or self.now_ms, "hash": "0x" + "0" * 64,
              "delta": {"type": "funding", "coin": coin, "usdc": _s(_q6(-szi * mid * rate)), "szi": _s(szi),
                        "fundingRate": _s(rate), "nSamples": 60}}
        acct.funding.append(ev)
        return ev


class FakeExchangeGateway:
    """Implements ``app.hl.client.ExchangeGateway`` over ``FakeHyperliquid``. Enforces what the real exchange
    would: builder present, platform cloid (as our SDK gateway does), grid when a catalog is set, reduce-only,
    duplicate cloids."""

    def __init__(self, hl: FakeHyperliquid, account_address: str, vault_address: str | None = None, *,
                 builder: BuilderCode) -> None:
        if not isinstance(builder, BuilderCode):
            raise ValidationFailed("builder code required")
        self.hl = hl
        self.master = account_address.lower()
        self.trading = (vault_address or account_address).lower()
        self.builder = builder

    def place_ioc(self, *, coin: str, is_buy: bool, sz: Decimal | str, limit_px: Decimal | str, reduce_only: bool,
                  cloid: str) -> OrderResult:
        hl, acct = self.hl, self.hl.account(self.trading)
        sz_d, px_d = Decimal(str(sz)), Decimal(str(limit_px))
        if not is_platform_cloid(cloid):
            raise ValidationFailed("cloid must carry the platform prefix")
        cloid = cloid.lower()
        if hl.catalog is not None:
            m = hl.catalog.market(coin)
            if not is_valid_sz(sz_d, m.sz_decimals) or not is_valid_px(px_d, m.sz_decimals):
                raise ValidationFailed("off-grid order", coin=coin)
        hl.tick()
        hl.orders_log.append({"coin": coin, "is_buy": is_buy, "sz": canonical(sz_d), "limit_px": canonical(px_d),
                              "reduce_only": reduce_only, "cloid": cloid, "builder": self.builder.wire(),
                              "account": self.master, "vault": self.trading if self.trading != self.master else None,
                              "tif": "Ioc", "time": hl.now_ms})
        b = hl._take_behavior(coin)
        if b.kind == "raise":
            raise FakeTransportError("connection reset (scripted)")
        if b.kind == "err_status":
            return normalize_order_response({"status": "err", "response": b.error or "error"}, sz_d)
        if cloid in acct.orders:
            return normalize_order_response(_order_err("Duplicate cloid."), sz_d)  # message UNVERIFIED
        hl._next_oid += 1
        oid = hl._next_oid
        order = {"coin": coin, "side": "B" if is_buy else "A", "limitPx": canonical(px_d), "sz": canonical(sz_d),
                 "oid": oid, "timestamp": hl.now_ms, "origSz": canonical(sz_d), "tif": "Ioc", "cloid": cloid,
                 "reduceOnly": reduce_only, "orderType": "Limit"}
        wrapper = {"order": order, "status": "open", "statusTimestamp": hl.now_ms}
        acct.orders[cloid] = wrapper
        if b.kind == "reject":
            wrapper["status"] = "rejected"
            return normalize_order_response(_order_err(b.error or "Order rejected."), sz_d)
        if b.kind == "resting":
            return normalize_order_response({"status": "ok", "response": {"type": "order", "data": {
                "statuses": [{"resting": {"oid": oid, "cloid": cloid}}]}}}, sz_d)
        fill_px = b.px if b.px is not None else hl.mids.get(coin, px_d)
        crosses = fill_px <= px_d if is_buy else fill_px >= px_d
        if b.kind == "no_match" or not crosses:
            wrapper["status"] = "canceled"
            asset = hl.catalog.asset_id(coin) if hl.catalog is not None and coin in hl.catalog else 0
            return normalize_order_response(_order_err(
                f"Order could not immediately match against any resting orders. asset={asset}"), sz_d)
        qty = sz_d
        pos = hl.position(self.trading, coin)
        if reduce_only:
            if pos == 0 or (pos > 0) == is_buy:
                wrapper["status"] = "reduceOnlyRejected"
                return normalize_order_response(_order_err("Reduce only order would increase position."), sz_d)
            qty = min(qty, abs(pos))
        if b.kind == "partial":
            ratio = b.ratio if b.ratio is not None else Decimal("0.5")
            qty = qty * ratio
            if hl.catalog is not None:
                qty = Decimal(hl.catalog.format_sz(coin, qty))
        if qty <= 0:
            wrapper["status"] = "canceled"
            return normalize_order_response(_order_err("Order could not immediately match against any resting "
                                                        "orders."), sz_d)
        hl.apply_fill(self.trading, coin, is_buy, qty, fill_px, cloid=cloid, oid=oid, builder=self.builder)
        order["sz"] = canonical(sz_d - qty)
        wrapper["status"] = "filled" if qty == sz_d else "canceled"
        resp = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": canonical(qty), "avgPx": _s(fill_px), "oid": oid}}]}}}
        if b.kind == "raise_after_fill":
            raise FakeTransportError("read timeout after execution (scripted)")
        return normalize_order_response(resp, sz_d)

    def update_leverage(self, *, coin: str, leverage: int, is_cross: bool) -> LeverageResult:
        if self.hl.catalog is not None:
            m = self.hl.catalog.market(coin)
            if is_cross and not m.allows_cross:
                raise ValidationFailed("market is isolated-only", coin=coin)
            if not 1 <= leverage <= m.max_leverage:
                raise ValidationFailed("leverage out of range", coin=coin)
        self.hl.account(self.trading).leverage[coin] = (leverage, is_cross)
        self.hl.leverage_log.append({"coin": coin, "leverage": leverage, "is_cross": is_cross,
                                     "account": self.trading})
        return LeverageResult(ok=True, raw={"status": "ok", "response": {"type": "default"}})


def _order_err(msg: str) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": msg}]}}}


class FakeGatewayFactory:
    """``GatewayFactory`` port. Records the keys it was handed (tests assert no plaintext leaks elsewhere)."""

    def __init__(self, hl: FakeHyperliquid, builder: BuilderCode) -> None:
        self.hl = hl
        self.builder = builder
        self.created: list[tuple[str, str | None]] = []

    def create(self, key: Any, account_address: str, vault_address: str | None) -> FakeExchangeGateway:
        if key is None:
            raise ValidationFailed("agent key required")
        self.created.append((account_address.lower(), vault_address.lower() if vault_address else None))
        return FakeExchangeGateway(self.hl, account_address, vault_address, builder=self.builder)


class FakeInfo:
    """Same public methods as ``InfoClient``. Market data from fixtures (overridable); account data from ``hl``."""

    def __init__(self, hl: FakeHyperliquid | None = None, *, fixtures: bool = True) -> None:
        self.hl = hl or FakeHyperliquid()
        self.calls: list[tuple[str, tuple]] = []
        self.fail_next: list[Exception] = []
        self._perp_dexs = load_fixture("perpDexs") if fixtures else [None]
        self._meta: dict[str, dict] = {}
        self._mac: dict[str, tuple[dict, list]] = {}
        if fixtures:
            self._meta = {"": load_fixture("meta"), "xyz": load_fixture("meta_xyz")}
            m, c = load_fixture("metaAndAssetCtxs")
            mx, cx = load_fixture("metaAndAssetCtxs_xyz")
            self._mac = {"": (m, c), "xyz": (mx, cx)}

    def _call(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.fail_next:
            raise self.fail_next.pop(0)

    def set_meta_and_asset_ctxs(self, dex: str, meta: dict, ctxs: list) -> None:
        self._mac[dex] = (meta, ctxs)
        self._meta[dex] = meta

    def set_ctx(self, coin: str, **fields: str | None) -> None:
        """Patch one market's ctx (e.g. markPx, midPx=None) in the cached metaAndAssetCtxs."""
        dex = coin.split(":", 1)[0] if ":" in coin else ""
        meta, ctxs = self._mac[dex]
        i = next(i for i, u in enumerate(meta["universe"]) if u["name"] == coin)
        ctxs = [dict(c) for c in ctxs]
        ctxs[i].update(fields)
        self._mac[dex] = (meta, ctxs)

    # market data ------------------------------------------------------------------------------------------
    def perp_dexs(self) -> list:
        self._call("perp_dexs")
        return self._perp_dexs

    def meta(self, dex: str = "") -> dict:
        self._call("meta", dex)
        if dex not in self._meta:
            raise ExternalServiceError("unknown dex", dex=dex)
        return self._meta[dex]

    def meta_and_asset_ctxs(self, dex: str = "") -> tuple[dict, list]:
        self._call("meta_and_asset_ctxs", dex)
        if dex not in self._mac:
            raise ExternalServiceError("unknown dex", dex=dex)
        return self._mac[dex]

    def candle_snapshot(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        self._call("candle_snapshot", coin, interval, start_ms, end_ms)
        name = {"BTC": "candleSnapshot_BTC_1d", "xyz:SILVER": "candleSnapshot_xyz_SILVER_1d"}.get(coin)
        rows = load_fixture(name) if name and interval == "1d" else []
        return [r for r in rows if start_ms <= r["t"] <= end_ms]

    def l2_book(self, coin: str) -> dict:
        self._call("l2_book", coin)
        name = {"BTC": "l2Book_BTC", "xyz:SILVER": "l2Book_xyz_SILVER"}.get(coin)
        if not name:
            raise NotFound("no book fixture", coin=coin)
        return load_fixture(name)

    # account data -----------------------------------------------------------------------------------------
    def user_fills(self, user: str) -> list[dict]:
        self._call("user_fills", user)
        return list(reversed(self.hl.account(normalize_address(user)).fills))[:2000]

    def user_fills_by_time(self, user: str, start_ms: int, end_ms: int | None = None, *,
                           aggregate_by_time: bool = False) -> list[dict]:
        self._call("user_fills_by_time", user, start_ms, end_ms)
        return [f for f in self.hl.account(normalize_address(user)).fills
                if f["time"] >= start_ms and (end_ms is None or f["time"] <= end_ms)][:2000]

    def user_funding(self, user: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        self._call("user_funding", user, start_ms, end_ms)
        return [f for f in self.hl.account(normalize_address(user)).funding
                if f["time"] >= start_ms and (end_ms is None or f["time"] <= end_ms)]

    def user_non_funding_ledger_updates(self, user: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        self._call("user_non_funding_ledger_updates", user, start_ms, end_ms)
        return [x for x in self.hl.account(normalize_address(user)).ledger
                if x["time"] >= start_ms and (end_ms is None or x["time"] <= end_ms)]

    def clearinghouse_state(self, user: str, dex: str = "") -> dict:
        self._call("clearinghouse_state", user, dex)
        acct = self.hl.account(normalize_address(user))
        aps = []
        for coin, p in acct.positions.items():
            if p.szi == 0 or (coin.split(":", 1)[0] if ":" in coin else "") != dex:
                continue
            mark = self.hl.mids.get(coin, p.entry_px)
            lev, cross = acct.leverage.get(coin, (1, True))
            aps.append({"type": "oneWay", "position": {
                "coin": coin, "szi": _s(p.szi), "entryPx": _s(p.entry_px),
                "positionValue": _s(_q6(abs(p.szi) * mark)), "unrealizedPnl": _s(_q6((mark - p.entry_px) * p.szi)),
                "leverage": {"type": "cross" if cross else "isolated", "value": lev}}})
        return {"assetPositions": aps, "marginSummary": {}, "crossMarginSummary": {}, "withdrawable": "0.0",
                "time": self.hl.now_ms}

    def order_status(self, user: str, oid_or_cloid: int | str) -> dict:
        self._call("order_status", user, oid_or_cloid)
        acct = self.hl.account(normalize_address(user))
        for cloid, w in acct.orders.items():
            if (isinstance(oid_or_cloid, str) and cloid == oid_or_cloid.lower()) or w["order"]["oid"] == oid_or_cloid:
                return {"status": "order", "order": json.loads(json.dumps(w))}
        return {"status": "unknownOid"}

    def extra_agents(self, user: str) -> list[dict]:
        self._call("extra_agents", user)
        return list(self.hl.agents.get(normalize_address(user), []))

    def user_role(self, user: str) -> dict:
        self._call("user_role", user)
        return self.hl.roles.get(normalize_address(user), {"role": "user"})

    def max_builder_fee(self, user: str, builder: str) -> int:
        self._call("max_builder_fee", user, builder)
        return self.hl.builder_fees.get((normalize_address(user), normalize_address(builder)), 0)

    def sub_accounts(self, user: str) -> list[dict]:
        self._call("sub_accounts", user)
        u = normalize_address(user)
        return [{"subAccountUser": a, "master": u} for a, r in self.hl.roles.items()
                if r.get("role") == "subAccount" and (r.get("data") or {}).get("master") == u]

    def frontend_open_orders(self, user: str, dex: str = "") -> list[dict]:
        self._call("frontend_open_orders", user, dex)
        acct = self.hl.account(normalize_address(user))
        return [w["order"] for w in acct.orders.values() if w["status"] == "open"]


def seed_approvals(hl: FakeHyperliquid, *, master: str, agent: str, builder: str, fee_tenths_bp: int = 100,
                   name: str = "aijalon", valid_until_ms: int | None = None,
                   sub_accounts: Iterable[str] = ()) -> None:
    """Convenience for API/executor tests: master approved our agent + builder fee; optional sub-accounts."""
    m, a = master.lower(), agent.lower()
    hl.agents.setdefault(m, []).append({"name": name, "address": a,
                                         "validUntil": valid_until_ms or hl.now_ms + 150 * 86_400_000})
    hl.roles[a] = {"role": "agent", "data": {"user": m}}
    hl.builder_fees[(m, builder.lower())] = fee_tenths_bp
    for s in sub_accounts:
        hl.roles[s.lower()] = {"role": "subAccount", "data": {"master": m}}

