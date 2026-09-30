"""Billing / money-state rules the API enforces (security-fix round; docs/security/REVIEW_AUTH_API.md F3 F4 F5,
REVIEW_MONEY.md H3 H4 H5 M1 M6 L1). No FastAPI imports (unit-testable against a real database or the fakes).

* Unpause (F3/H3): restore the billing state saved at pause time; if the paid period ended while paused, the
  renewal is charged NOW (same ledger key as the settlement job, so it can never be charged twice) or the unpause
  is refused with 402 — a pause/unpause cycle never re-activates an unpaid subscription or restarts its grace.
* Delisting (H5): trading subscriptions → ``closing`` (reduce-only exit, then cancelled; billing stops), paused /
  pending ones → ``cancelled`` (positions left as they are); every user gets a mandatory ``strategy_ended`` alert.
* Chargeback / refund (M6, L1): a reversal that leaves the fee balance negative moves the user's billable
  subscriptions to ``reduce_only`` at once; a full reversal marks the deposit ``reversed``.
* Money out (F4/H4, F5, M1): the withdrawable amount is the USDC-funded unspent balance (card lot spent first),
  minus the accrued-but-unsettled profit share and the per-subscription reserve; creator/referrer earnings that were
  paid from card-funded spending are held for the card dispute window; nothing leaves to a wallet verified < 48 h ago
  or while the account is under a security hold (MFA change, new device / new country sign-in).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.domain import billing
from app.errors import Conflict, Forbidden, InsufficientBalance
from app.money import BPS

from . import ledger_ops

__all__ = [
    "PAYOUT_ADDRESS_HOLD", "SECURITY_HOLD", "CARD_DISPUTE_HOLD", "MAX_POST_PRICE_MICRO",
    "renewal_key", "charge_subscription_renewal", "resume_subscription", "end_strategy_subscriptions",
    "after_payment_reversal", "payout_holds", "require_no_payout_hold", "accrued_profit_share",
    "require_withdrawal_headroom", "payout_available",
]

PAYOUT_ADDRESS_HOLD = timedelta(hours=48)     # F5: a newly verified wallet cannot receive money for 48 h
SECURITY_HOLD = timedelta(hours=48)           # F5: no money out for 48 h after an MFA change / new device sign-in
CARD_DISPUTE_HOLD = timedelta(days=120)       # H4: card network dispute window
MAX_POST_PRICE_MICRO = 500_000_000            # F2: paid posts are capped at $500


def payout_address_hold(settings: Any) -> timedelta:
    return timedelta(hours=int(getattr(settings, "payout_address_hold_hours", 48) or 48))


def security_hold(settings: Any) -> timedelta:
    return timedelta(hours=int(getattr(settings, "security_hold_hours", 48) or 48))


def card_dispute_hold(settings: Any) -> timedelta:
    econ = getattr(settings, "economics", None)
    return timedelta(days=int(getattr(econ, "card_dispute_hold_days", 0) or getattr(settings, "card_dispute_hold_days", 0)
                              or 120))


def max_post_price_micro(settings: Any) -> int:
    econ = getattr(settings, "economics", None)
    return int(getattr(econ, "post_max_price_micro", 0) or getattr(settings, "post_max_price_micro", 0)
               or MAX_POST_PRICE_MICRO)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------------- renewals / unpause
def renewal_key(subscription_id: str, period_end: datetime) -> str:
    """IDENTICAL to app.execution.settlement.renewal_key: the API and the settlement job share one ledger key per
    (subscription, period), so a renewal can never be charged twice."""
    return f"sub:{subscription_id}:{_utc(period_end).date().isoformat()}"


def charge_subscription_renewal(conn: Any, svc: Any, *, user_id: str, subscription_id: str, strategy: dict,
                                price_micro: int, period_end: datetime, actor: str) -> Optional[str]:
    """The renewal of the period that ended at ``period_end`` (same split and ledger key as the settlement job)."""
    if price_micro <= 0:
        return None
    creator_part, platform_part = svc.domain.subscription_split(price_micro)
    owner = strategy.get("owner_user_id")
    entries: list[tuple[str, int]] = [(ledger_ops.fee_balance(user_id), price_micro)]
    if strategy.get("in_house") or not owner:
        entries.append((ledger_ops.ACC_SUBSCRIPTION_REVENUE, -(creator_part + platform_part)))
    else:
        entries += [(ledger_ops.creator_payable(str(owner)), -creator_part),
                    (ledger_ops.ACC_SUBSCRIPTION_REVENUE, -platform_part)]
    return ledger_ops.post(conn, svc, key=renewal_key(subscription_id, period_end), kind="subscription_renewal",
                           memo=f"subscription renewal period_end={_utc(period_end).isoformat()}", created_by=actor,
                           entries=entries)


def resume_subscription(conn: Any, svc: Any, *, user_id: str, sub_id: str, actor: str) -> dict:
    """Un-pause (caller holds the user lock). Returns {status, past_due_since, charged_micro, period_end}.
    Raises Conflict when the strategy is not listed, InsufficientBalance when a renewal fell due while paused and the
    balance cannot pay it."""
    row = svc.store.subscription_billing_row(conn, sub_id, user_id)
    if row is None or row["status"] != "paused_user":
        raise Conflict("this subscription is not paused")
    st = svc.store.get_strategy(conn, str(row["strategy_id"]))
    if st is None or st["status"] != "listed":
        raise Conflict("the strategy is not listed any more; it cannot be resumed", reason="strategy_not_listed",
                       strategy_status=(st or {}).get("status"))
    now = svc.now()
    grace = int(svc.settings.economics.past_due_grace_hours)
    prev = row.get("pre_pause_status") or billing.ACTIVE
    balance = ledger_ops.spendable(conn, svc, user_id)
    price = row.get("pinned_price_micro")
    price = int(price if price is not None else (st.get("price_monthly_micro") or 0))
    period_end = row.get("current_period_end")
    renewal_due = prev != billing.PENDING and period_end is not None and period_end <= now
    due = price if renewal_due else 0
    decision = billing.resume_after_pause(prev, row.get("pre_pause_past_due_since"), balance, due, now, grace)
    charged, new_end = 0, None
    if renewal_due:
        if decision.status != billing.ACTIVE:
            raise InsufficientBalance("the paid period ended while paused; top up to resume",
                                      balance_micro=balance, required_micro=max(due, 0),
                                      reason="renewal_due")
        charge_subscription_renewal(conn, svc, user_id=user_id, subscription_id=sub_id, strategy=st, price_micro=due,
                                    period_end=period_end, actor=actor)
        charged = due
        new_end = billing.next_renewal_after(row["created_at"], max(now, period_end))
    if not svc.store.resume_subscription(conn, sub_id, status=decision.status, past_due_since=decision.past_due_since,
                                         period_end=new_end):
        raise Conflict("subscription state changed; reload")
    return {"status": decision.status, "past_due_since": decision.past_due_since, "charged_micro": charged,
            "period_end": new_end, "restored_from": prev}


# ---------------------------------------------------------------------------------------------- delisting (H5)
def end_strategy_subscriptions(conn: Any, svc: Any, *, strategy_id: str, strategy_name: Optional[str]) -> dict:
    """Delisting: every live subscription ends (see module doc). Returns counts for the audit log."""
    rows = svc.store.end_subscriptions_of_strategy(conn, strategy_id, svc.now())
    closing = cancelled = 0
    for r in rows:
        positions = "closing" if r["status"] == "closing" else "left_open"
        closing += positions == "closing"
        cancelled += positions != "closing"
        svc.notifier.notify(conn, user_id=str(r["user_id"]), severity="critical", kind="strategy_ended",
                            payload={"strategy_id": strategy_id, "strategy": strategy_name,
                                     "subscription_id": str(r["id"]), "positions": positions,
                                     "reason": "strategy_delisted"},
                            dedup_key=f"strategy_ended:{r['id']}")
    return {"closing": closing, "cancelled": cancelled}


# ---------------------------------------------------------------------------------------------- reversals (M6, L1)
def after_payment_reversal(conn: Any, svc: Any, instr: Any) -> list[dict]:
    """Right after a refund / chargeback was posted (same transaction). Returns the subscriptions restricted."""
    meta = dict(getattr(instr, "meta", {}) or {})
    pi = meta.get("payment_intent")
    if meta.get("full_reversal") and isinstance(pi, str) and pi:
        svc.store.mark_deposit_reversed_ref(conn, pi, instr.user_id)
    if ledger_ops.spendable(conn, svc, instr.user_id) >= 0:
        return []
    rows = svc.store.restrict_after_reversal(conn, instr.user_id, svc.now())
    for r in rows:
        svc.notifier.notify(conn, user_id=instr.user_id, severity="warn", kind="subscription_reduce_only",
                            payload={"subscription_id": str(r["id"]), "strategy_id": str(r["strategy_id"]),
                                     "reason": getattr(instr, "kind", "payment_reversal")},
                            dedup_key=f"reversal_reduce_only:{r['id']}:{instr.idempotency_key}")
    return rows


# ---------------------------------------------------------------------------------------------- money out
def payout_holds(conn: Any, svc: Any, *, user_id: str, to_address: str) -> tuple[list[str], dict]:
    """(reasons, context). reasons ⊂ {payout_address_hold, security_hold}; context for the admin view."""
    now = svc.now()
    ctx = svc.store.payout_context(conn, user_id, to_address, now - timedelta(days=30))
    reasons: list[str] = []
    verified = ctx.get("to_address_verified_at")
    if verified is None or now - verified < payout_address_hold(svc.settings):
        reasons.append("payout_address_hold")
    hold = ctx.get("security_hold_until")
    if hold is not None and hold > now:
        reasons.append("security_hold")
    return reasons, ctx


def require_no_payout_hold(conn: Any, svc: Any, *, user_id: str, to_address: str) -> None:
    reasons, ctx = payout_holds(conn, svc, user_id=user_id, to_address=to_address)
    if "payout_address_hold" in reasons:
        verified = ctx.get("to_address_verified_at")
        raise Forbidden("this wallet was verified less than 48 hours ago; it can receive money after the hold",
                        reason="payout_address_hold",
                        until=(verified + payout_address_hold(svc.settings)).isoformat() if verified else None)
    if "security_hold" in reasons:
        raise Forbidden("money transfers are on hold for 48 hours after a security change on your account",
                        reason="security_hold", until=ctx["security_hold_until"].isoformat())


def accrued_profit_share(conn: Any, svc: Any, user_id: str) -> int:
    """Profit share accrued on attributed PnL that the daily settlement has not charged yet (M1)."""
    econ = svc.settings.economics
    platform = int(econ.platform_profit_share_bps)
    on_top = getattr(econ, "platform_profit_share_mode", "on_top") == "on_top"
    total = 0
    for r in svc.store.accrued_profit_share_inputs(conn, user_id):
        creator = int(r.get("profit_share_bps") or 0)
        rate = creator + platform if on_top else creator
        cum = int(r.get("cum_pnl_micro") or 0) + int(r.get("realized_micro") or 0) + int(r.get("funding_micro") or 0)
        profit = max(0, cum - int(r.get("hwm_micro") or 0))
        total += profit * rate // BPS
    return total


def require_withdrawal_headroom(conn: Any, svc: Any, *, user_id: str, amount_micro: int) -> None:
    """M1: a fee-balance withdrawal must leave enough to pay the accrued profit share plus the per-subscription
    reserve, and is refused while any subscription is past_due / reduce_only."""
    rows = svc.store.accrued_profit_share_inputs(conn, user_id)
    if any(r.get("status") in ("past_due", "reduce_only") for r in rows):
        raise Conflict("settle the overdue subscription first", reason="subscription_past_due")
    econ = svc.settings.economics
    live = [r for r in rows if r.get("status") in ("active", "paused_user", "closing")]
    reserve = int(econ.min_topup_micro) * sum(
        1 for r in live if int(r.get("profit_share_bps") or 0) > 0 or int(econ.platform_profit_share_bps) > 0)
    accrued = accrued_profit_share(conn, svc, user_id)
    spendable = ledger_ops.spendable(conn, svc, user_id)
    if spendable - amount_micro < accrued + reserve:
        raise InsufficientBalance("keep enough balance for accrued profit share and the subscription reserve",
                                  balance_micro=spendable, accrued_profit_share_micro=accrued,
                                  reserve_micro=reserve,
                                  max_withdrawal_micro=max(0, spendable - accrued - reserve))


def payout_available(conn: Any, svc: Any, account: str) -> dict:
    """Creator / referrer payable: balance, part held because it was paid from card-funded spending inside the
    dispute window (H4), and what may be requested now."""
    balance = -svc.ledger.balance(conn, account)
    held = svc.store.payable_card_held(conn, account, svc.now() - card_dispute_hold(svc.settings))
    return {"balance_micro": balance, "held_micro": held, "available_micro": max(0, balance - held)}
