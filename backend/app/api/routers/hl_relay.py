"""POST /v1/hl/exchange-relay — fallback relay for USER-SIGNED Hyperliquid actions (SPEC §6; app/hl/relay.py).

The browser posts the user's signed ``approveAgent`` / ``approveBuilderFee`` / ``usdSend`` (to our treasury) directly
to Hyperliquid ``/exchange``; only when that fails at the network/CORS level does ``web/src/core/hl.ts`` send the
same body here. Signed-in + MFA (``consented_user``), per-user rate limit, audit-logged before and after forwarding.
The body is validated field by field (``app.hl.relay.validate_relay``: allowed types only, our chain / builder /
fee cap / treasury / the caller's PENDING agent, fresh nonce, signer = one of the caller's VERIFIED wallets) and then
forwarded unchanged. Orders, cancels, withdrawals and every other action type are refused (422) and never sent.
The response carries Hyperliquid's own status and body so the web can interpret it exactly as a direct call.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, get_services, user_limit

router = APIRouter(tags=["hyperliquid"])


@router.post("/hl/exchange-relay", response_model=S.HlRelayOut,
             dependencies=[user_limit("hl_relay", 20, 3600)])
def exchange_relay(body: S.HlRelayIn, ctx: AuthCtx = Depends(consented_user),
                   svc: Services = Depends(get_services)) -> S.HlRelayOut:
    from app.hl.relay import RelayPolicy, validate_relay

    s = svc.settings
    raw = {"action": dict(body.action), "nonce": int(body.nonce), "signature": body.signature.model_dump()}
    with svc.db.begin() as conn:
        wallets = [str(w["address"]).lower() for w in svc.store.list_wallets(conn, ctx.user_id) if w.get("verified_at")]
        pending = {str(a["agent_address"]).lower(): {"master_address": str(a["master_address"]).lower(),
                                                     "agent_name": a["agent_name"]}
                   for a in svc.store.list_agents(conn, ctx.user_id) if a.get("status") == "pending_approval"}
    policy = RelayPolicy(hyperliquid_chain="Mainnet" if s.hl_is_mainnet else "Testnet",
                         builder_address=(s.builder_address or "").lower(),
                         max_builder_fee_tenths_bp=int(s.economics.builder_fee_tenths_bp),
                         treasury_address=(s.treasury_address or "").lower())
    check = validate_relay(raw, policy=policy, now_ms=int(svc.now().timestamp() * 1000), verified_wallets=wallets,
                           pending_agents=pending)
    with svc.db.begin() as conn:
        svc.audit.write(conn, actor=ctx.actor, action=f"hl.relay.{check.kind}", target=f"wallet:{check.signer}",
                        payload=check.detail, ip_hash=ctx.ip_hash)
    status, resp = svc.hl.relay_exchange(check.body)
    with svc.db.begin() as conn:
        svc.audit.write(conn, actor=ctx.actor, action=f"hl.relay.{check.kind}.result", target=f"wallet:{check.signer}",
                        payload={"nonce": check.nonce, "upstream_status": status,
                                 "ok": isinstance(resp, dict) and resp.get("status") == "ok"}, ip_hash=ctx.ip_hash)
    return S.HlRelayOut(upstream_status=status, response=resp)
