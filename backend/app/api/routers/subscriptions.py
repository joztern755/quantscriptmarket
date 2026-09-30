"""Subscriptions: GET list/one, POST (step-up + Idempotency-Key), PATCH (step-up), DELETE (step-up, SPEC §12).

Create checks (all fail closed):
  * strategy listed, priced (price and profit share set; $0 is a valid, explicit price — SILVER), has a
    published version; client's expected terms (if sent) equal the current ones;
  * a fresh subscription_ack for THIS strategy at the current version: inline `ack`, or recorded via
    POST /consents within the last 30 minutes (the web subscribe gate);
  * trading address = a verified wallet of the user, or a Hyperliquid sub-account whose master is one;
  * our agent is ACTIVE for that master; the builder-fee approval (checked live on-chain) ≥ required fee;
  * no other live subscription on the trading address (also a DB unique index);
  * plan allows one more strategy; launch-phase caps (per-user / platform allocation, max leverage);
  * leverage ≤ min(platform cap, the version's MAX_LEVERAGE, launch cap);
  * fee balance ≥ first charge + reserve. DECISION: first charge = the monthly price (prepaid now; creator 97% /
    platform 3%); reserve = economics.min_topup_micro whenever profit share can accrue (creator % > 0 or the
    platform's 1.5% > 0), so the first daily settlement can be paid. For SILVER ($0, 0% creator) the platform
    share still applies → a $10 balance is required, nothing is charged at subscribe.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    consented_user,
    decode_cursor_or_422,
    get_services,
    idempotency_key,
    next_cursor,
    run_idempotent,
    step_up_user,
    user_limit,
)
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed
from app.alerts.user_sinks import require_alert_contacts

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

ACK_FRESHNESS = timedelta(minutes=30)
_CHANGEABLE = ("pending", "active", "past_due", "reduce_only", "paused_user")


def _out(row: dict) -> S.SubscriptionOut:
    return S.SubscriptionOut(
        id=row["id"], strategy_id=row["strategy_id"], strategy_slug=row.get("strategy_slug"),
        strategy_name=row.get("strategy_name"), strategy_markets=list(row.get("strategy_markets") or []),
        trading_address=row["trading_address"],
        allocation_micro=int(row["allocation_micro"]), max_leverage_x100=int(row["max_leverage_x100"]),
        status=row["status"], cancel_positions=row.get("cancel_positions"), cancelled_at=row.get("cancelled_at"),
        current_period_end=row.get("current_period_end"),
        cum_pnl_micro=int(row.get("cum_pnl_micro") or 0), hwm_micro=int(row.get("hwm_micro") or 0),
        created_at=row["created_at"])


def _leverage_cap_x100(svc: Services, version: Optional[dict]) -> int:
    cap = svc.settings.risk.platform_max_leverage * 100
    if version and version.get("max_leverage"):
        cap = min(cap, int(version["max_leverage"]) * 100)
    launch = svc.config.launch.max_user_leverage_x100
    return min(cap, launch) if launch else cap


def _check_allocation_caps(conn: Any, svc: Services, user_id: str, new_total_for_user: int, delta: int) -> None:
    launch = svc.config.launch
    if launch.max_allocation_per_user_micro is not None and new_total_for_user > launch.max_allocation_per_user_micro:
        raise Forbidden("allocation above the launch-phase per-user cap",
                        cap_micro=launch.max_allocation_per_user_micro)
    if launch.max_total_platform_allocation_micro is not None and delta > 0:
        platform = svc.store.total_live_allocation(conn)
        if platform + delta > launch.max_total_platform_allocation_micro:
            raise Forbidden("the platform-wide launch allocation cap is reached; try again later")


def _resolve_master(conn: Any, svc: Services, user_id: str, trading_address: str) -> str:
    """Trading address must be a verified wallet of the user or a sub-account of one (checked on-chain)."""
    if svc.store.verified_wallet(conn, user_id, trading_address) is not None:
        return trading_address
    master = svc.hl.master_of(trading_address)
    if master and svc.store.verified_wallet(conn, user_id, master) is not None:
        return master
    raise Forbidden("the trading address must be your verified wallet or one of its Hyperliquid sub-accounts")


@router.get("", response_model=S.Page[S.SubscriptionOut])
def list_subscriptions(limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                       ctx: AuthCtx = Depends(consented_user),
                       svc: Services = Depends(get_services)) -> S.Page[S.SubscriptionOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_subscriptions(conn, ctx.user_id, limit, cur), limit)
    return S.Page[S.SubscriptionOut](items=[_out(r) for r in rows], next_cursor=nxt)


@router.get("/{sub_id}", response_model=S.SubscriptionOut)
def get_subscription(sub_id: UUID, ctx: AuthCtx = Depends(consented_user),
                     svc: Services = Depends(get_services)) -> S.SubscriptionOut:
    with svc.db.begin() as conn:
        row = svc.store.get_subscription(conn, str(sub_id), ctx.user_id)
    if row is None:
        raise NotFound("subscription not found")
    return _out(row)


@router.post("", response_model=S.SubscriptionCreateOut, status_code=201,
             dependencies=[user_limit("subscribe", 10, 3600)])
def create_subscription(body: S.SubscriptionCreateIn, ctx: AuthCtx = Depends(step_up_user),
                        key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    s, econ, cfg = svc.settings, svc.settings.economics, svc.config
    sid, addr = str(body.strategy_id), body.trading_address
    allocation = int(body.allocation_micro)  # normalised by the schema

    # ---- network checks first (no DB transaction held open during Hyperliquid calls)
    with svc.db.begin() as conn:
        require_alert_contacts(conn, svc, ctx.user_id)   # SPEC §12: Telegram linked + email confirmed (409 contacts_required)
        master = _resolve_master(conn, svc, ctx.user_id, addr)
    if not s.builder_address:
        raise ValidationFailed("builder address not configured")
    approved = svc.hl.max_builder_fee(master, s.builder_address)
    if approved < econ.builder_fee_tenths_bp:
        raise Conflict("approve the builder fee in your wallet first", reason="builder_fee_not_approved",
                       approved_tenths_bp=approved, required_tenths_bp=econ.builder_fee_tenths_bp)

    def work(conn: Any) -> S.SubscriptionCreateOut:
        user = svc.store.get_user(conn, ctx.user_id, for_update=True)   # serialises this user's money moves
        st = svc.store.get_strategy(conn, sid)
        if st is None or st["status"] != "listed":
            raise NotFound("strategy not available")
        if st["price_monthly_micro"] is None or st["profit_share_bps"] is None:
            raise Conflict("strategy is not priced yet")
        price, ps_bps = int(st["price_monthly_micro"]), int(st["profit_share_bps"])
        if body.expected_price_monthly_micro is not None and body.expected_price_monthly_micro != price:
            raise Conflict("the price changed; please review again", reason="terms_changed", price_monthly_micro=price)
        if body.expected_profit_share_bps is not None and body.expected_profit_share_bps != ps_bps:
            raise Conflict("the profit share changed; please review again", reason="terms_changed",
                           profit_share_bps=ps_bps)
        version = svc.store.current_versions(conn, [sid]).get(sid)
        if version is None:
            raise Conflict("strategy has no published version yet")

        ack_version = cfg.legal_versions["subscription_ack"]
        if body.ack is not None:
            a = body.ack
            if a.version != ack_version:
                raise Conflict("the risk acknowledgement was updated; please review again", current_version=ack_version)
            canonical = cfg.legal_doc_hashes.get("subscription_ack")
            if canonical and a.doc_text_sha256 != canonical:
                raise Conflict("the acknowledgement text changed; please reload")
            quoted = (a.quoted_price_monthly_micro, a.quoted_profit_share_bps, a.quoted_platform_profit_share_bps,
                      a.quoted_builder_fee_tenths_bp)
            if quoted != (price, ps_bps, econ.platform_profit_share_bps, econ.builder_fee_tenths_bp):
                raise Conflict("fees changed since you reviewed them; please review again")
            svc.store.insert_consent(conn, user_id=ctx.user_id, doc="subscription_ack", version=a.version,
                                     doc_text_sha256=a.doc_text_sha256, context="subscribe", strategy_id=sid,
                                     ip_hash=ctx.ip_hash, ua_hash=ctx.ua_hash)
        elif svc.store.recent_subscription_ack(conn, user_id=ctx.user_id, strategy_id=sid, version=ack_version,
                                               since=svc.now() - ACK_FRESHNESS) is None:
            raise Conflict("acknowledge this strategy's risks and fees first", reason="subscription_ack_required")

        if body.max_leverage_x100 > _leverage_cap_x100(svc, version):
            raise ValidationFailed("leverage above the allowed maximum", max_x100=_leverage_cap_x100(svc, version))
        live = svc.store.count_live_subscriptions(conn, ctx.user_id)
        if not svc.domain.plan_allows(user["plan"], live + 1):
            raise Forbidden("your plan's strategy limit is reached; upgrade or cancel one", reason="plan_limit",
                            plan=user["plan"])
        _check_allocation_caps(conn, svc, ctx.user_id,
                               svc.store.total_live_allocation(conn, ctx.user_id) + allocation, allocation)
        if svc.store.active_agent_for_master(conn, ctx.user_id, master) is None:
            raise Conflict("approve the trading agent in your wallet first", reason="agent_not_active")
        if svc.store.live_subscription_on_address(conn, addr) is not None:
            raise Conflict("this trading address already runs a strategy; use a sub-account",
                           reason="trading_address_in_use")
        reserve = econ.min_topup_micro if (ps_bps > 0 or econ.platform_profit_share_bps > 0) else 0
        ledger_ops.require_balance(conn, svc, ctx.user_id, price + reserve)

        now = svc.now()
        row = svc.store.insert_subscription(
            conn, user_id=ctx.user_id, strategy_id=sid, version_id=str(version["id"]), trading_address=addr,
            master_address=master, allocation_micro=allocation, max_leverage_x100=body.max_leverage_x100,
            status="active", current_period_end=svc.domain.add_months(now, 1))
        charged, tx = ledger_ops.charge_subscription_start(conn, svc, user_id=ctx.user_id,
                                                           subscription_id=str(row["id"]), strategy=st,
                                                           actor=ctx.actor)
        svc.audit.write(conn, actor=ctx.actor, action="subscription.create", target=f"subscription:{row['id']}",
                        payload={"strategy_id": sid, "version": version["version"], "allocation_micro": allocation,
                                 "max_leverage_x100": body.max_leverage_x100, "charged_micro": charged,
                                 "ledger_tx": tx}, ip_hash=ctx.ip_hash)
        return S.SubscriptionCreateOut(subscription=_out(row), charged_micro=charged,
                                       fee_balance_micro=ledger_ops.spendable(conn, svc, ctx.user_id))

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope="POST /subscriptions", payload=body, work=work,
                          status_code=201)


@router.patch("/{sub_id}", response_model=S.SubscriptionOut, dependencies=[user_limit("sub_patch", 20, 3600)])
def patch_subscription(sub_id: UUID, body: S.SubscriptionPatchIn, ctx: AuthCtx = Depends(step_up_user),
                       svc: Services = Depends(get_services)) -> S.SubscriptionOut:
    with svc.db.begin() as conn:
        svc.store.lock_user(conn, ctx.user_id)
        row = svc.store.get_subscription(conn, str(sub_id), ctx.user_id, for_update=True)
        if row is None:
            raise NotFound("subscription not found")
        if row["status"] not in _CHANGEABLE:
            raise Conflict("this subscription can no longer be changed", status=row["status"])
        changes: dict[str, Any] = {}
        if body.max_leverage_x100 is not None:
            version = svc.store.get_version(conn, str(row["strategy_version_id"]))
            cap = _leverage_cap_x100(svc, version)
            if body.max_leverage_x100 > cap:
                raise ValidationFailed("leverage above the allowed maximum", max_x100=cap)
            changes["max_leverage_x100"] = body.max_leverage_x100
        if body.allocation_micro is not None:
            delta = int(body.allocation_micro) - int(row["allocation_micro"])
            others = svc.store.total_live_allocation(conn, ctx.user_id, exclude_subscription_id=str(sub_id))
            _check_allocation_caps(conn, svc, ctx.user_id, others + int(body.allocation_micro), delta)
            changes["allocation_micro"] = int(body.allocation_micro)
        status = None
        if body.paused is True and row["status"] != "paused_user":
            status = "paused_user"
        elif body.paused is False and row["status"] == "paused_user":
            if svc.store.live_subscription_on_address(conn, row["trading_address"]) is not None:
                raise Conflict("another subscription now uses this trading address")
            if svc.store.active_agent_for_master(conn, ctx.user_id, row["master_address"] or row["trading_address"]) is None:
                raise Conflict("approve the trading agent in your wallet first", reason="agent_not_active")
            status = "active"   # billing job moves it to past_due/reduce_only if the balance is short
        if status:
            changes["status"] = status
        svc.store.update_subscription(conn, str(sub_id), allocation_micro=changes.get("allocation_micro"),
                                      max_leverage_x100=changes.get("max_leverage_x100"), status=status)
        svc.audit.write(conn, actor=ctx.actor, action="subscription.update", target=f"subscription:{sub_id}",
                        payload={"changes": changes, "from": {"allocation_micro": int(row["allocation_micro"]),
                                                             "max_leverage_x100": int(row["max_leverage_x100"]),
                                                             "status": row["status"]}}, ip_hash=ctx.ip_hash)
        row = svc.store.get_subscription(conn, str(sub_id), ctx.user_id)
    return _out(row)


@router.delete("/{sub_id}", response_model=S.SubscriptionOut, dependencies=[user_limit("sub_cancel", 20, 3600)])
def cancel_subscription(sub_id: UUID, body: S.SubscriptionCancelIn, ctx: AuthCtx = Depends(step_up_user),
                        svc: Services = Depends(get_services)) -> S.SubscriptionOut:
    """SPEC §12: body {"positions": "close"|"leave"} is required. close → 'closing' (executor flattens
    reduce-only, then 'cancelled'); leave → 'cancelled' now, the executor never touches the account again for it.
    The prepaid period is not refunded; the agent stays approved unless the user revokes it."""
    with svc.db.begin() as conn:
        row = svc.store.get_subscription(conn, str(sub_id), ctx.user_id, for_update=True)
        if row is None:
            raise NotFound("subscription not found")
        if row["status"] == "cancelled":
            raise Conflict("already cancelled")
        if row["status"] == "closing" and body.positions == "close":
            return _out(row)
        updated = svc.store.end_subscription(conn, str(sub_id), positions=body.positions, now=svc.now())
        if updated is None:
            raise Conflict("already cancelled")
        svc.audit.write(conn, actor=ctx.actor, action="subscription.cancel", target=f"subscription:{sub_id}",
                        payload={"positions": body.positions, "from_status": row["status"],
                                 "to_status": updated["status"]}, ip_hash=ctx.ip_hash)
        row = svc.store.get_subscription(conn, str(sub_id), ctx.user_id)
    return _out(row)
