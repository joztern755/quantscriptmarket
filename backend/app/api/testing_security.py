"""In-memory fakes of the security-fix store methods (app/api/store_security.py) — TEST-ONLY, mixed into
app.api.testing.FakeStore. Same semantics as the SQL, simplified: the card-lot / held-earnings functions of 0011
are exercised against a real database in tests/test_fix_api_money_db.py."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


class FakeSecurityStoreMixin:
    w: Any

    # subscriptions
    def subscription_billing_row(self, conn: Any, sub_id: str, user_id: str) -> Optional[dict]:
        s = self.w.subscriptions.get(sub_id)
        if not s or s["user_id"] != user_id:
            return None
        return {"id": s["id"], "user_id": s["user_id"], "strategy_id": s["strategy_id"], "status": s["status"],
                "past_due_since": s.get("past_due_since"), "current_period_end": s.get("current_period_end"),
                "created_at": s["created_at"], "pre_pause_status": s.get("pre_pause_status"),
                "pre_pause_past_due_since": s.get("pre_pause_past_due_since"),
                "pinned_price_micro": s.get("price_monthly_micro"),
                "pinned_profit_share_bps": s.get("profit_share_bps"), "cancelled_at": s.get("cancelled_at")}

    def pause_subscription(self, conn: Any, sub_id: str) -> Optional[dict]:
        s = self.w.subscriptions[sub_id]
        if s["status"] not in ("pending", "active", "past_due", "reduce_only"):
            return None
        s.update(pre_pause_status=s["status"], pre_pause_past_due_since=s.get("past_due_since"), status="paused_user")
        return {"id": sub_id, "status": "paused_user", "pre_pause_status": s["pre_pause_status"]}

    def resume_subscription(self, conn: Any, sub_id: str, *, status: str, past_due_since: Optional[datetime],
                            period_end: Optional[datetime]) -> bool:
        s = self.w.subscriptions[sub_id]
        if s["status"] != "paused_user":
            return False
        s.update(status=status, past_due_since=past_due_since, pre_pause_status=None, pre_pause_past_due_since=None)
        if period_end is not None:
            s["current_period_end"] = period_end
        return True

    def end_subscriptions_of_strategy(self, conn: Any, strategy_id: str, now: datetime) -> list[dict]:
        out = []
        for s in self.w.subscriptions.values():
            if s["strategy_id"] != strategy_id or s["status"] not in ("pending", "active", "past_due", "reduce_only",
                                                                       "paused_user"):
                continue
            trading = s["status"] in ("active", "past_due", "reduce_only")
            s.update(status="closing" if trading else "cancelled", cancel_positions="close" if trading else "leave",
                     end_reason="strategy_delisted", pre_pause_status=None)
            if not trading:
                s["cancelled_at"] = now
            out.append({"id": s["id"], "user_id": s["user_id"], "status": s["status"]})
        return out

    def restrict_after_reversal(self, conn: Any, user_id: str, now: datetime) -> list[dict]:
        out = []
        for s in self.w.subscriptions.values():
            if s["user_id"] == user_id and s["status"] in ("active", "past_due"):
                s.update(status="reduce_only", past_due_since=s.get("past_due_since") or now)
                out.append({"id": s["id"], "strategy_id": s["strategy_id"], "status": "reduce_only"})
            elif s["user_id"] == user_id and s["status"] == "paused_user" and s.get("pre_pause_status") in ("active",
                                                                                                            "past_due"):
                s.update(pre_pause_status="reduce_only",
                         pre_pause_past_due_since=s.get("pre_pause_past_due_since") or now)
        return out

    def subscribed_seconds(self, conn: Any, user_id: str, strategy_id: str, now: datetime) -> int:
        total = 0
        for s in self.w.subscriptions.values():
            if s["user_id"] != user_id or s["strategy_id"] != strategy_id or s["status"] == "pending":
                continue
            end = s.get("cancelled_at") if s["status"] == "cancelled" else now
            total += max(0, int(((end or now) - s["created_at"]).total_seconds()))
        return total

    def accrued_profit_share_inputs(self, conn: Any, user_id: str) -> list[dict]:
        out = []
        for s in self.w.subscriptions.values():
            if s["user_id"] != user_id or s["status"] not in ("active", "past_due", "reduce_only", "paused_user",
                                                               "closing"):
                continue
            st = self.w.strategies[s["strategy_id"]]
            out.append({"id": s["id"], "status": s["status"], "cum_pnl_micro": s.get("cum_pnl_micro", 0),
                        "hwm_micro": s.get("hwm_micro", 0), "in_house": st["in_house"],
                        "profit_share_bps": s.get("profit_share_bps", st.get("profit_share_bps") or 0),
                        "realized_micro": s.get("unsettled_pnl_micro", 0), "funding_micro": 0})
        return out

    # funding source
    def card_unspent(self, conn: Any, user_id: str) -> int:
        return 0

    def payable_card_held(self, conn: Any, account_code: str, since: datetime) -> int:
        return int(getattr(self.w, "card_held", {}).get(account_code, 0))

    def usdc_credited_minus_withdrawn(self, conn: Any, user_id: str) -> int:
        return self.withdrawable_usdc(conn, user_id)   # type: ignore[attr-defined]

    # security holds
    def extend_security_hold(self, conn: Any, user_id: str, until: datetime) -> None:
        u = self.w.users[user_id]
        cur = u.get("security_hold_until")
        u["security_hold_until"] = max(cur, until) if cur else until

    def payout_context(self, conn: Any, user_id: str, to_address: str, since: datetime) -> dict:
        w = self.w.wallets.get(to_address)
        verified = w["verified_at"] if w and w["user_id"] == user_id else None
        events = [{"action": a["action"], "created_at": a.get("at") or self.w.now} for a in reversed(self.w.audit)
                  if a["actor"] in (f"user:{user_id}", f"admin:{user_id}")
                  and a["action"] in ("auth.new_device", "auth.new_country", "auth.mfa_changed", "wallet.verify",
                                      "alerts.email_verified", "alerts.email_confirmed")][:10]
        return {"to_address_verified_at": verified,
                "security_hold_until": self.w.users.get(user_id, {}).get("security_hold_until"), "events": events}

    # referrals
    def record_ip_net(self, conn: Any, user_id: str, net_hash: str) -> bool:
        nets = self.w.__dict__.setdefault("ip_nets", set())
        new = (user_id, net_hash) not in nets
        nets.add((user_id, net_hash))
        return new

    def mark_device_id(self, conn: Any, user_id: str, device_hash: str) -> None:
        self.w.__dict__.setdefault("id_devices", set()).add((user_id, device_hash))

    def set_device_fp_hash(self, conn: Any, user_id: str, device_hash: str) -> None:
        u = self.w.users[user_id]
        if not u.get("device_fp_hash"):
            u["device_fp_hash"] = device_hash

    def referral_identity(self, conn: Any, user_id: str, since: datetime) -> dict:
        u = self.w.users.get(user_id, {})
        devices = {d for (uid, d) in self.w.__dict__.get("id_devices", set()) if uid == user_id}
        if u.get("device_fp_hash"):
            devices.add(u["device_fp_hash"])
        return {"wallets": [w["address"] for w in self.w.wallets.values() if w["user_id"] == user_id],
                "devices": sorted(devices),
                "networks": sorted(n for (uid, n) in self.w.__dict__.get("ip_nets", set()) if uid == user_id)}

    def flag_referral(self, conn: Any, user_id: str, reason: str, now: datetime) -> None:
        u = self.w.users[user_id]
        u["referral_flagged_at"] = u.get("referral_flagged_at") or now
        u["referral_flag_reason"] = reason

    # maker-checker
    def cancel_pending_changes(self, conn: Any, target: str, reason: str, now: datetime,
                               kinds: tuple[str, ...] = ("strategy_list", "strategy_price")) -> list[dict]:
        out = []
        for ch in self.w.changes.values():
            if ch["target"] == target and ch["status"] == "pending" and ch["kind"] in kinds:
                ch.update(status="cancelled", decided_at=now)
                out.append({"id": ch["id"], "kind": ch["kind"]})
        return out

    # payouts
    def set_payout_send(self, conn: Any, kind: str, payout_id: str, *, nonce: int, admin_id: str,
                        now: datetime) -> Optional[dict]:
        r = (self.w.withdrawals if kind == "withdrawal" else self.w.payouts).get(payout_id)
        if r is None:
            return None
        if r.get("send_nonce") is None:
            if r["status"] != "approved_2":
                return None
            r.update(send_nonce=nonce, send_issued_at=now)
        return {"send_nonce": r["send_nonce"], "send_issued_at": r["send_issued_at"]}

    def claim_payout_tx_hash(self, conn: Any, tx_hash: str, kind: str, payout_id: str) -> bool:
        claimed = self.w.__dict__.setdefault("payout_tx_hashes", {})
        if tx_hash.lower() in claimed:
            return False
        claimed[tx_hash.lower()] = (kind, payout_id)
        return True

    # deposits
    def mark_deposit_reversed_ref(self, conn: Any, payment_intent: str, user_id: str) -> int:
        d = self.w.deposits.get(payment_intent)
        if d and d["user_id"] == user_id and d["status"] == "credited":
            d["status"] = "reversed"
            return 1
        return 0

    # strategies / posts / change queue (routes touched by the fixes that the base fakes did not cover)
    def set_strategy_status(self, conn: Any, strategy_id: str, status: str) -> None:
        self.w.strategies[str(strategy_id)]["status"] = status

    def set_strategy_price(self, conn: Any, strategy_id: str, price_monthly_micro: int) -> None:
        self.w.strategies[str(strategy_id)]["price_monthly_micro"] = price_monthly_micro

    def update_strategy_terms(self, conn: Any, strategy_id: str, *, name: Any, description: Any,
                              price_monthly_micro: Any, profit_share_bps: Any) -> None:
        st = self.w.strategies[str(strategy_id)]
        for k, val in (("name", name), ("description", description), ("price_monthly_micro", price_monthly_micro),
                       ("profit_share_bps", profit_share_bps)):
            if val is not None:
                st[k] = val

    def add_post(self, *, creator_id: str, price_micro: int, strategy_id: Optional[str] = None) -> dict:
        import uuid
        posts = self.w.__dict__.setdefault("posts", {})
        row = {"id": str(uuid.uuid4()), "created_at": self.w.now, "creator_id": creator_id, "strategy_id": strategy_id,
               "title": "Post", "body": "body", "price_micro": price_micro, "published_at": self.w.now,
               "strategy_slug": None, "strategy_in_house": None}
        posts[row["id"]] = row
        return row

    def get_post(self, conn: Any, post_id: str) -> Optional[dict]:
        p = self.w.__dict__.get("posts", {}).get(str(post_id))
        return dict(p) if p else None

    def has_purchased(self, conn: Any, post_id: str, user_id: str) -> bool:
        return (str(post_id), user_id) in self.w.__dict__.get("purchases", set())

    def insert_purchase(self, conn: Any, *, post_id: str, user_id: str, price_micro: int, tx_id: str) -> bool:
        purchases = self.w.__dict__.setdefault("purchases", set())
        if (str(post_id), user_id) in purchases:
            return False
        purchases.add((str(post_id), user_id))
        return True

    def insert_change(self, conn: Any, *, kind: str, target: str, payload: dict, reason: str, maker: str) -> dict:
        import uuid
        row = {"id": str(uuid.uuid4()), "created_at": self.w.now, "kind": kind, "target": target, "payload": payload,
               "reason": reason, "status": "pending", "maker_admin": maker, "checker_admin": None, "decided_at": None}
        self.w.changes[row["id"]] = row
        return dict(row)

    def open_change_for(self, conn: Any, kind: str, target: str) -> Optional[dict]:
        return next(({"id": c["id"]} for c in self.w.changes.values()
                     if c["kind"] == kind and c["target"] == target and c["status"] == "pending"), None)

    def get_change(self, conn: Any, change_id: str, *, for_update: bool = True) -> Optional[dict]:
        c = self.w.changes.get(str(change_id))
        return dict(c) if c else None

    def decide_change(self, conn: Any, change_id: str, *, status: str, checker: str, now: datetime,
                      decision_reason: str) -> Optional[dict]:
        c = self.w.changes.get(str(change_id))
        if c is None or c["status"] != "pending" or str(c["maker_admin"]) == str(checker):
            return None
        c.update(status=status, checker_admin=checker, decided_at=now)
        return dict(c)
