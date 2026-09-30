"""GET /positions — live from Hyperliquid (clearinghouseState) for the user's own trading addresses.

Builder-deployed (HIP-3) perps live in their own clearinghouse per dex, so every dex that any of the user's
subscribed strategies trades (e.g. "xyz" for xyz:SILVER) is queried as well as the validator dex ("").
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, get_services, user_limit
from app.errors import AppError

router = APIRouter(tags=["positions"])

MAX_ADDRESSES = 10


def _s(v: object) -> str | None:
    return None if v is None else str(v)


@router.get("/positions", response_model=S.PositionsOut, dependencies=[user_limit("positions", 30, 60)])
def positions(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.PositionsOut:
    with svc.db.begin() as conn:
        addresses = svc.store.trading_addresses(conn, ctx.user_id)[:MAX_ADDRESSES]
        subs = svc.store.list_subscriptions(conn, ctx.user_id, 100, None)
    dexes = {""} | {m.split(":", 1)[0] for sub in subs for m in (sub.get("strategy_markets") or []) if ":" in m}
    out: list[S.PositionOut] = []
    unavailable: list[str] = []
    for addr in addresses:
        try:
            for dex in sorted(dexes):
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
    return S.PositionsOut(positions=out, unavailable=unavailable)
