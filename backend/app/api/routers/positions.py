"""GET /positions — live from Hyperliquid (clearinghouseState) for the user's own trading addresses.

Builder-deployed (HIP-3) perps live in their own clearinghouse per dex, so every dex that any of the user's
subscribed strategies trades (e.g. "xyz" for xyz:SILVER) is queried as well as the validator dex ("").

Hyperliquid budget (REVIEW_AUTH_API F1): the response is cached per user for ``CACHE_SECONDS`` in this process, the
work per request is bounded (``MAX_ADDRESSES`` addresses × dexes, at most ``MAX_HL_CALLS`` calls; only dexes on the
trusted allowlist are queried), and every call is charged to the shared per-IP budget by ``HlInfoAdapter``. Addresses
that could not be read (budget spent, over the call cap, Hyperliquid error) are listed in ``unavailable``.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, get_services, user_limit
from app.errors import AppError

router = APIRouter(tags=["positions"])

MAX_ADDRESSES = 5
MAX_DEXES = 6              # validator + 5 builder dexes
MAX_HL_CALLS = 15          # per uncached request
CACHE_SECONDS = 15.0
_CACHE_MAX_USERS = 10_000

_cache: "OrderedDict[str, tuple[float, S.PositionsOut]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cached(user_id: str) -> "S.PositionsOut | None":
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(user_id)
        if hit is None:
            return None
        if now - hit[0] > CACHE_SECONDS:
            _cache.pop(user_id, None)
            return None
        return hit[1]


def _store(user_id: str, out: S.PositionsOut) -> None:
    with _cache_lock:
        _cache[user_id] = (time.monotonic(), out)
        _cache.move_to_end(user_id)
        while len(_cache) > _CACHE_MAX_USERS:
            _cache.popitem(last=False)


def _s(v: object) -> str | None:
    return None if v is None else str(v)


@router.get("/positions", response_model=S.PositionsOut, dependencies=[user_limit("positions", 30, 60)])
def positions(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.PositionsOut:
    hit = _cached(ctx.user_id)
    if hit is not None:
        return hit
    with svc.db.begin() as conn:
        all_addresses = svc.store.trading_addresses(conn, ctx.user_id)
        subs = svc.store.list_subscriptions(conn, ctx.user_id, 100, None)
        try:
            trusted = svc.store.trusted_dexes(conn)
        except Exception:  # noqa: BLE001 - allowlist unreadable: validator dex only (never an attacker-named dex)
            trusted = frozenset({""})
    addresses = all_addresses[:MAX_ADDRESSES]
    wanted = {m.split(":", 1)[0] for sub in subs for m in (sub.get("strategy_markets") or []) if ":" in m}
    dexes = [""] + sorted(d for d in wanted if d in trusted)[:MAX_DEXES - 1]
    out: list[S.PositionOut] = []
    unavailable: list[str] = list(all_addresses[MAX_ADDRESSES:])
    calls = 0
    for addr in addresses:
        if calls + len(dexes) > MAX_HL_CALLS:
            unavailable.append(addr)
            continue
        try:
            for dex in dexes:
                calls += 1
                state = svc.hl.clearinghouse_state(addr, dex)
                for ap in state.get("assetPositions", []) or []:
                    p = ap.get("position") or {}
                    if not p.get("coin") or str(p.get("szi", "0")) in ("0", "0.0"):
                        continue
                    lev = p.get("leverage") or {}
                    out.append(S.PositionOut(trading_address=addr, coin=str(p["coin"]), size=str(p.get("szi")),
                                             entry_px=_s(p.get("entryPx")), position_value=_s(p.get("positionValue")),
                                             unrealized_pnl=_s(p.get("unrealizedPnl")),
                                             leverage=_s(lev.get("value") if isinstance(lev, dict) else lev),
                                             liquidation_px=_s(p.get("liquidationPx"))))
        except AppError:
            unavailable.append(addr)
    result = S.PositionsOut(positions=out, unavailable=unavailable)
    _store(ctx.user_id, result)
    return result
