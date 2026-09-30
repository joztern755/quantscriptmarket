"""Money movements initiated by the API, expressed as balanced double-entry postings.

Sign convention (migrations/0001_init.sql): amount_micro + = DEBIT, − = CREDIT. A user's fee balance is a
liability, so spending it is a DEBIT (+) on `user:{id}:fee_balance` and topping it up is a CREDIT (−).

Every posting is idempotent on its ledger key (the DB's ledger_post() returns the existing tx for a repeated key
with identical content and raises AJ409 for different content). Keys used here never collide with the
settlement job's keys (`sub:{id}:{period_end}`, `plan:{uid}:{period_end}`, `ps:…`, `bf:…`).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.errors import InsufficientBalance
from app.logging import get_logger

log = get_logger("app.api.ledger")

ACC_SUBSCRIPTION_REVENUE = "platform:revenue:subscription"
ACC_POSTS_REVENUE = "platform:revenue:posts"
ACC_PLANS_REVENUE = "platform:revenue:plans"
ACC_WITHDRAWALS_PENDING = "withdrawals:pending"   # liability: user money on its way out (maker-checker)
ACC_PAYOUTS_PENDING = "payouts:pending"           # liability: creator/referrer money on its way out
ACC_TREASURY = "treasury:hl_usdc"
ACC_STRIPE_CLEARING = "stripe:clearing"


def fee_balance(user_id: str) -> str:
    return f"user:{user_id}:fee_balance"


def creator_payable(user_id: str) -> str:
    return f"creator:{user_id}:payable"


def referrer_payable(user_id: str) -> str:
    return f"referrer:{user_id}:payable"


def account_spec(code: str) -> tuple[str, bool, Optional[str]]:
    """code → (kind, non_negative, owner_user_id). Unknown shapes raise (never guess an account's nature)."""
    parts = code.split(":")
    if len(parts) == 3 and parts[0] == "user" and parts[2] == "fee_balance":
        return "liability", True, parts[1]
    if len(parts) == 3 and parts[0] in ("creator", "referrer") and parts[2] == "payable":
        return "liability", True, parts[1]
    if parts[0] == "platform" and len(parts) >= 3 and parts[1] == "revenue":
        return "revenue", False, None
    if code in (ACC_TREASURY, ACC_STRIPE_CLEARING, "builder:hl_receivable"):
        return "asset", False, None
    if code in (ACC_WITHDRAWALS_PENDING, ACC_PAYOUTS_PENDING, "suspense:usdc_unmatched"):
        return "liability", False, None
    raise ValueError(f"unknown ledger account shape: {code}")


def post(conn: Any, svc: Any, *, key: str, kind: str, memo: str, created_by: str,
         entries: list[tuple[str, int]]) -> str:
    lines = [(a, int(m)) for a, m in entries if m]
    if len(lines) < 2 or sum(m for _, m in lines) != 0:
        raise AssertionError("refusing to post an unbalanced or empty ledger transaction")
    for code in sorted({a for a, _ in lines}):
        svc.ledger.ensure_account(conn, code)
    return svc.ledger.post(conn, idempotency_key=key, kind=kind, memo=memo, entries=lines, created_by=created_by)


def spendable(conn: Any, svc: Any, user_id: str) -> int:
    return -svc.ledger.balance(conn, fee_balance(user_id))


def require_balance(conn: Any, svc: Any, user_id: str, needed_micro: int) -> int:
    """Caller MUST hold the user row lock (store.lock_user) so check → post is race-free."""
    bal = spendable(conn, svc, user_id)
    if bal < needed_micro:
        raise InsufficientBalance("fee balance too low", balance_micro=bal, required_micro=needed_micro)
    return bal


# ---------------------------------------------------------------------------------------------- charges
def charge_subscription_start(conn: Any, svc: Any, *, user_id: str, subscription_id: str, strategy: dict,
                              actor: str) -> tuple[int, Optional[str]]:
    """First period, prepaid at subscribe. Creator 97% / platform 3% (in-house: all platform)."""
    price = int(strategy.get("price_monthly_micro") or 0)
    if price <= 0:
        return 0, None
    creator_part, platform_part = svc.domain.subscription_split(price)
    owner = strategy.get("owner_user_id")
    entries: list[tuple[str, int]] = [(fee_balance(user_id), price)]
    if strategy.get("in_house") or not owner:
        entries.append((ACC_SUBSCRIPTION_REVENUE, -(creator_part + platform_part)))
    else:
        entries += [(creator_payable(str(owner)), -creator_part), (ACC_SUBSCRIPTION_REVENUE, -platform_part)]
    tx = post(conn, svc, key=f"sub:{subscription_id}:start", kind="subscription_start",
              memo=f"subscription {strategy.get('slug')} first period", created_by=actor, entries=entries)
    return price, tx


def charge_plan(conn: Any, svc: Any, *, user_id: str, plan: str, period_start: datetime, actor: str) -> tuple[int, Optional[str]]:
    price = svc.domain.plan_price(plan)
    if price <= 0:
        return 0, None
    tx = post(conn, svc, key=f"plan:{user_id}:start:{plan}:{period_start.date().isoformat()}", kind="plan_purchase",
              memo=f"plan {plan} first month", created_by=actor,
              entries=[(fee_balance(user_id), price), (ACC_PLANS_REVENUE, -price)])
    return price, tx


def charge_post(conn: Any, svc: Any, *, user_id: str, post_row: dict, actor: str) -> tuple[int, str]:
    """Paid post: creator price − $1, platform $1 (in-house strategy post: all platform)."""
    price = int(post_row["price_micro"])
    creator_part, platform_part = svc.domain.post_sale_split(price)
    entries: list[tuple[str, int]] = [(fee_balance(user_id), price)]
    if post_row.get("strategy_in_house"):
        entries.append((ACC_POSTS_REVENUE, -price))
    else:
        entries += [(creator_payable(str(post_row["creator_id"])), -creator_part), (ACC_POSTS_REVENUE, -platform_part)]
    tx = post(conn, svc, key=f"post:{post_row['id']}:{user_id}", kind="post_purchase",
              memo=f"post {post_row['id']}", created_by=actor, entries=entries)
    return price, tx


# ---------------------------------------------------------------------------------------------- withdrawals / payouts
def hold_withdrawal(conn: Any, svc: Any, *, user_id: str, withdrawal_id: str, amount: int, actor: str) -> str:
    return post(conn, svc, key=f"withdrawal:{withdrawal_id}:hold", kind="withdrawal_hold",
                memo="fee-balance withdrawal requested", created_by=actor,
                entries=[(fee_balance(user_id), amount), (ACC_WITHDRAWALS_PENDING, -amount)])


def hold_payout(conn: Any, svc: Any, *, source_account: str, payout_id: str, amount: int, actor: str) -> str:
    return post(conn, svc, key=f"payout:{payout_id}:hold", kind="payout_hold", memo="payout requested",
                created_by=actor, entries=[(source_account, amount), (ACC_PAYOUTS_PENDING, -amount)])


def release_hold(conn: Any, svc: Any, *, kind: str, row: dict, source_account: str, actor: str) -> str:
    """Rejected → money goes back to where it came from."""
    pending = ACC_WITHDRAWALS_PENDING if kind == "withdrawal" else ACC_PAYOUTS_PENDING
    amount = int(row["amount_micro"])
    return post(conn, svc, key=f"{kind}:{row['id']}:release", kind=f"{kind}_release", memo=f"{kind} rejected",
                created_by=actor, entries=[(pending, amount), (source_account, -amount)])


def settle_sent(conn: Any, svc: Any, *, kind: str, row: dict, tx_hash: str, actor: str) -> str:
    """USDC left the treasury on Hyperliquid: pending liability ↓, treasury asset ↓."""
    pending = ACC_WITHDRAWALS_PENDING if kind == "withdrawal" else ACC_PAYOUTS_PENDING
    amount = int(row["amount_micro"])
    return post(conn, svc, key=f"{kind}:{row['id']}:sent", kind=f"{kind}_sent", memo=f"usdSend {tx_hash}",
                created_by=actor, entries=[(pending, amount), (ACC_TREASURY, -amount)])


# ---------------------------------------------------------------------------------------------- payment instructions
def apply_credit(conn: Any, svc: Any, instr: Any, *, actor: str) -> dict:
    """payments.CreditInstruction → ONE ledger tx + deposits row (idempotent on instr.idempotency_key)."""
    tx = post(conn, svc, key=instr.idempotency_key, kind=instr.kind, memo=instr.memo or "deposit", created_by=actor,
              entries=[(instr.debit_account, instr.amount_micro), (instr.credit_account, -instr.amount_micro)])
    return svc.store.mark_deposit_credited(conn, user_id=instr.user_id, method=instr.method,
                                           external_ref=instr.external_ref, amount_micro=instr.amount_micro, tx_id=tx)


def apply_debit(conn: Any, svc: Any, instr: Any, *, actor: str) -> Optional[str]:
    """payments.DebitInstruction (refund / dispute). The fee-balance account is non-negative in the DB; if the
    user has already spent the money the posting is refused (AJ402) — we then raise a critical ops alert for
    manual recovery instead of failing the webhook forever. A SAVEPOINT isolates the failed attempt."""
    try:
        with conn.begin_nested():
            tx = post(conn, svc, key=instr.idempotency_key, kind=instr.kind, memo=instr.memo or instr.kind,
                      created_by=actor,
                      entries=[(instr.debit_account, instr.amount_micro), (instr.credit_account, -instr.amount_micro)])
    except InsufficientBalance:
        svc.notifier.notify(conn, user_id=None, severity="critical", kind="payment_reversal_unrecovered",
                            payload={"user_id": instr.user_id, "amount_micro": instr.amount_micro,
                                     "external_ref": instr.external_ref, "kind": instr.kind})
        log.error("payment reversal exceeds fee balance", extra={"fields": {
            "user_id": instr.user_id, "external_ref": instr.external_ref, "amount_micro": instr.amount_micro}})
        return None
    return tx
