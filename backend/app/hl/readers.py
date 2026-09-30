"""Info-endpoint adapters for the execution ports and on-chain approval checks (no keys involved).

- ``HlMarketData``      → ``app.execution.ports.MarketData``      (cached metaAndAssetCtxs per dex)
- ``HlPositionReader``  → ``app.execution.ports.PositionReader``  (clearinghouseState per dex)
- ``HlOrderStatusReader`` → ``app.execution.ports.OrderStatusReader`` (orderStatus by cloid + fills by oid)
- ``verify_agent_approval`` / ``verify_builder_approval`` / ``verify_trading_address`` for the API confirm
  endpoints (SPEC §8): they read chain state, never trust the client.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Callable, Iterable, Mapping

from app.errors import ExternalServiceError, GuardRejected, NotFound, ValidationFailed
from app.execution.ports import MarketSnapshot as PortSnapshot
from app.execution.ports import PlaceResult, Position
from app.hl.info import normalize_address
from app.hl.markets import MarketCatalog
from app.logging import get_logger
from app.money import parse_decimal, to_micro

__all__ = [
    "HlMarketData", "HlPositionReader", "HlOrderStatusReader", "AgentApproval", "BuilderApproval",
    "verify_agent_approval", "verify_builder_approval", "verify_trading_address", "order_status_to_result",
]

log = get_logger("app.hl.readers")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HlMarketData:
    """Catalog cache: perpDexs refreshed every ``dex_ttl_s``; metaAndAssetCtxs per dex every ``ctx_ttl_s``.
    ``as_of`` is the local receive time of the ctx response (Hyperliquid does not timestamp ctxs)."""

    def __init__(self, info: Any, *, ctx_ttl_s: float = 15.0, dex_ttl_s: float = 3600.0,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._info = info
        self._ctx_ttl = ctx_ttl_s
        self._dex_ttl = dex_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._perp_dexs: list | None = None
        self._perp_dexs_at: datetime | None = None
        self._dex: dict[str, tuple[datetime, dict, list]] = {}

    def _fresh(self, at: datetime | None, ttl: float) -> bool:
        return at is not None and (self._clock() - at).total_seconds() < ttl

    def catalog(self, coins: Iterable[str] = ()) -> MarketCatalog:
        dexes = [""] + MarketCatalog.dexes_for(coins)
        with self._lock:
            if not self._fresh(self._perp_dexs_at, self._dex_ttl):
                self._perp_dexs, self._perp_dexs_at = self._info.perp_dexs(), self._clock()
            for d in dexes:
                cached = self._dex.get(d)
                if cached is None or not self._fresh(cached[0], self._ctx_ttl):
                    meta, ctx = self._info.meta_and_asset_ctxs(d)
                    self._dex[d] = (self._clock(), meta, ctx)
            used = {d: self._dex[d] for d in dexes}
            oldest = min(v[0] for v in used.values())
            return MarketCatalog.build(self._perp_dexs or [], {d: v[1] for d, v in used.items()},
                                       {d: v[2] for d, v in used.items()}, oldest)

    def snapshot(self, coin: str) -> PortSnapshot | None:
        try:
            snap = self.catalog([coin]).to_snapshot(coin)
        except (NotFound, GuardRejected) as e:
            log.info("hl_snapshot_unavailable", extra={"fields": {"coin": coin, "reason": e.message}})
            return None
        return PortSnapshot(coin=snap.coin, mid_px=snap.mid_px, mark_px=snap.mark_px, oracle_px=snap.oracle_px,
                            day_notional_volume_micro=snap.day_ntl_vlm_micro,
                            open_interest_notional_micro=snap.open_interest_micro, max_leverage=snap.max_leverage,
                            sz_decimals=snap.sz_decimals, as_of=snap.data_time, is_delisted=snap.is_delisted)


class HlPositionReader:
    def __init__(self, info: Any) -> None:
        self._info = info

    def positions(self, address: str, coins: Iterable[str]) -> Mapping[str, Position]:
        coins = list(coins)
        out = {c: Position.flat(c) for c in coins}
        for dex in sorted({c.split(":", 1)[0] if ":" in c else "" for c in coins}):
            state = self._info.clearinghouse_state(address, dex)
            for ap in state.get("assetPositions", []):
                p = ap.get("position") or {}
                coin = p.get("coin")
                if coin not in out:
                    continue
                szi = parse_decimal(p["szi"])
                value = to_micro(parse_decimal(p["positionValue"]), ROUND_FLOOR)  # |notional| at mark
                out[coin] = Position(coin=coin, szi=szi, notional_micro=value if szi >= 0 else -value)
        return out


def order_status_to_result(resp: Mapping[str, Any], fills: Iterable[Mapping[str, Any]] = ()) -> PlaceResult | None:
    """orderStatus payload → PlaceResult; None for ``unknownOid``. Statuses seen on mainnet: open, filled,
    canceled, perpMarginRejected, reduceOnlyRejected, oracleRejected, marginCanceled, reduceOnlyCanceled,
    siblingFilledCanceled, triggered. ``filled_sz`` = Σ fills of the oid when given (exact), else origSz − sz."""
    if resp.get("status") == "unknownOid":
        return None
    if resp.get("status") != "order":
        return PlaceResult(status="unknown", error=str(resp.get("status")), raw=dict(resp))
    wrapper = resp.get("order") or {}
    order = wrapper.get("order") or {}
    status = str(wrapper.get("status") or "")
    oid = order.get("oid")
    try:
        orig, remaining = parse_decimal(order["origSz"]), parse_decimal(order["sz"])
    except (KeyError, TypeError, ValueError):
        return PlaceResult(status="unknown", error="bad order payload", raw=dict(resp))
    mine = [f for f in fills if f.get("oid") == oid]
    avg_px = None
    if mine:
        filled = sum((parse_decimal(f["sz"]) for f in mine), Decimal(0))
        if filled > 0:
            avg_px = sum((parse_decimal(f["px"]) * parse_decimal(f["sz"]) for f in mine), Decimal(0)) / filled
    else:
        filled = max(Decimal(0), orig - remaining)
    if status == "open":
        return PlaceResult(status="resting", filled_sz=filled, avg_px=avg_px, oid=oid, raw=dict(resp))
    if status == "filled":
        st = "filled" if filled >= orig else "partial"
        return PlaceResult(status=st, filled_sz=filled, avg_px=avg_px, oid=oid, raw=dict(resp))
    low = status.lower()
    if any(k in low for k in ("rejected", "canceled", "cancelled")):
        st = "partial" if filled > 0 else "rejected"
        return PlaceResult(status=st, filled_sz=filled, avg_px=avg_px, oid=oid, error=status, raw=dict(resp))
    return PlaceResult(status="unknown", filled_sz=filled, avg_px=avg_px, oid=oid, error=status, raw=dict(resp))


class HlOrderStatusReader:
    def __init__(self, info: Any, *, fill_window_ms: int = 120_000) -> None:
        self._info = info
        self._window = fill_window_ms

    def order_status_by_cloid(self, address: str, cloid: str) -> PlaceResult | None:
        resp = self._info.order_status(address, cloid)
        if resp.get("status") != "order":
            return order_status_to_result(resp)
        ts = int(((resp.get("order") or {}).get("order") or {}).get("timestamp") or 0)
        fills: list = []
        if ts:
            try:
                fills = self._info.user_fills_by_time(address, max(0, ts - 1000), ts + self._window)
            except ExternalServiceError:
                fills = []  # fall back to origSz − sz
        return order_status_to_result(resp, fills)


# ------------------------------------------------------------------------------------------ approval checks

@dataclass(frozen=True)
class AgentApproval:
    approved: bool
    name: str | None
    valid_until_ms: int | None
    reason: str | None = None

    def expires_within(self, now_ms: int, days: int) -> bool:
        return self.valid_until_ms is not None and self.valid_until_ms - now_ms < days * 86_400_000


def verify_agent_approval(info: Any, master: str, agent_address: str, *, now_ms: int | None = None,
                          expected_name: str | None = "aijalon", min_validity_days: int = 7) -> AgentApproval:
    """Checks ``extraAgents(master)`` lists our agent (and, when given, under our name) with enough validity left,
    and cross-checks ``userRole(agent)`` = agent of this master. Named agents EXPIRE (``validUntil``); observed
    expiries were ≤ ~167 days ahead — schedule re-approval before expiry."""
    master, agent = normalize_address(master, "master"), normalize_address(agent_address, "agent")
    now = int(time.time() * 1000) if now_ms is None else now_ms
    entry = next((a for a in info.extra_agents(master) if str(a.get("address", "")).lower() == agent), None)
    if entry is None:
        return AgentApproval(False, None, None, "agent not approved by master")
    name, vu = entry.get("name"), entry.get("validUntil")
    vu = vu if isinstance(vu, int) and not isinstance(vu, bool) else None
    if expected_name is not None and name != expected_name:
        return AgentApproval(False, name, vu, f"agent approved under name {name!r}")
    if vu is not None and vu - now < min_validity_days * 86_400_000:
        return AgentApproval(False, name, vu, "agent approval expires too soon")
    role = info.user_role(agent)
    if role.get("role") != "agent" or str((role.get("data") or {}).get("user", "")).lower() != master:
        return AgentApproval(False, name, vu, "userRole does not confirm agent of this master")
    return AgentApproval(True, name, vu)


@dataclass(frozen=True)
class BuilderApproval:
    approved: bool
    max_fee_tenths_bp: int


def verify_builder_approval(info: Any, user: str, builder: str, *, required_tenths_bp: int = 100) -> BuilderApproval:
    """``maxBuilderFee(user, builder)`` must be ≥ the fee we attach to every order, else orders are rejected."""
    got = info.max_builder_fee(user, builder)
    return BuilderApproval(got >= required_tenths_bp, got)


def verify_trading_address(info: Any, master: str, trading_address: str) -> str:
    """Returns "master" or "subAccount". Refuses an address that is neither the master nor one of ITS
    sub-accounts (a user must never point our agent at someone else's account)."""
    master, trading = normalize_address(master, "master"), normalize_address(trading_address, "trading address")
    if trading == master:
        return "master"
    role = info.user_role(trading)
    if role.get("role") == "subAccount" and str((role.get("data") or {}).get("master", "")).lower() == master:
        return "subAccount"
    raise ValidationFailed("trading address is not a sub-account of this master")
