"""SQL for the API security-fix round (migrations/0011_api_fixes.sql; docs/security/REVIEW_AUTH_API.md,
REVIEW_MONEY.md). Mixed into ``app.api.store.SqlStore`` (same conventions: SQLAlchemy ``text()`` bound parameters
ONLY, one type context per parameter, plain dicts back; the helpers ``_one`` / ``_all`` / ``_exec`` live on
SqlStore).

Kept in its own module so the security fixes stay reviewable in one place and do not collide with concurrent edits
of store.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

SECURITY_EVENT_ACTIONS = ("auth.new_device", "auth.new_country", "auth.mfa_changed", "alerts.email_verified",
                          "alerts.email_confirmed", "alerts.telegram_link_created", "wallet.verify")
_BILLABLE = ("active", "past_due", "reduce_only")


class SecurityStoreMixin:
    # ------------------------------------------------------------------------------------ subscriptions (F3/H3, M8)
    def subscription_billing_row(self, conn: Any, sub_id: str, user_id: str) -> Optional[dict]:
        """Billing columns of ONE subscription of the user, row-locked (FOR UPDATE)."""
        return self._one(conn, """
            SELECT s.id, s.user_id, s.strategy_id, s.status::text AS status, s.past_due_since, s.current_period_end,
                   s.created_at, s.pre_pause_status::text AS pre_pause_status, s.pre_pause_past_due_since,
                   s.price_monthly_micro AS pinned_price_micro, s.profit_share_bps AS pinned_profit_share_bps,
                   s.cancelled_at
              FROM subscriptions s
             WHERE s.id = CAST(:id AS uuid) AND s.user_id = CAST(:u AS uuid)
             FOR UPDATE""", id=sub_id, u=user_id)

    def pause_subscription(self, conn: Any, sub_id: str) -> Optional[dict]:
        """User pause: remember the billing state (status + past_due_since) so unpause restores it exactly."""
        return self._one(conn, """
            UPDATE subscriptions
               SET pre_pause_status = status, pre_pause_past_due_since = past_due_since,
                   status = 'paused_user', status_changed_at = now()
             WHERE id = CAST(:id AS uuid) AND status IN ('pending', 'active', 'past_due', 'reduce_only')
            RETURNING id, status::text AS status, pre_pause_status::text AS pre_pause_status""", id=sub_id)

    def resume_subscription(self, conn: Any, sub_id: str, *, status: str, past_due_since: Optional[datetime],
                            period_end: Optional[datetime]) -> bool:
        return self._exec(conn, """
            UPDATE subscriptions
               SET status = CAST(:st AS subscription_status), past_due_since = CAST(:pds AS timestamptz),
                   current_period_end = coalesce(CAST(:pe AS timestamptz), current_period_end),
                   pre_pause_status = NULL, pre_pause_past_due_since = NULL, status_changed_at = now()
             WHERE id = CAST(:id AS uuid) AND status = 'paused_user'
            RETURNING id""", st=status, pds=past_due_since, pe=period_end, id=sub_id) > 0

    def end_subscriptions_of_strategy(self, conn: Any, strategy_id: str, now: datetime) -> list[dict]:
        """H5 (delisting): trading subscriptions → 'closing' (the executor flattens reduce-only, then cancels; billing
        never renews or reactivates 'closing'); pending / user-paused ones (the executor is not trading them) →
        'cancelled' with positions left as they are. Returns [{id, user_id, status (new)}]."""
        return self._all(conn, """
            UPDATE subscriptions s
               SET status = CASE WHEN s.status IN ('active', 'past_due', 'reduce_only')
                                 THEN CAST('closing' AS subscription_status)
                                 ELSE CAST('cancelled' AS subscription_status) END,
                   cancel_positions = CASE WHEN s.status IN ('active', 'past_due', 'reduce_only')
                                           THEN CAST('close' AS cancel_positions_mode)
                                           ELSE CAST('leave' AS cancel_positions_mode) END,
                   cancelled_at = CASE WHEN s.status IN ('active', 'past_due', 'reduce_only') THEN s.cancelled_at
                                       ELSE CAST(:t AS timestamptz) END,
                   end_reason = 'strategy_delisted', status_changed_at = CAST(:t2 AS timestamptz),
                   pre_pause_status = NULL, pre_pause_past_due_since = NULL
             WHERE s.strategy_id = CAST(:id AS uuid)
               AND s.status IN ('pending', 'active', 'past_due', 'reduce_only', 'paused_user')
            RETURNING s.id, s.user_id, s.status::text AS status""", t=now, t2=now, id=strategy_id)

    def restrict_after_reversal(self, conn: Any, user_id: str, now: datetime) -> list[dict]:
        """M6: a chargeback / refund left the fee balance negative → every billable subscription of the user goes
        reduce_only NOW (no new entries; exits run); a user-paused one will resume as reduce_only."""
        rows = self._all(conn, """
            UPDATE subscriptions
               SET status = 'reduce_only', past_due_since = coalesce(past_due_since, CAST(:t AS timestamptz)),
                   status_changed_at = CAST(:t2 AS timestamptz)
             WHERE user_id = CAST(:u AS uuid) AND status IN ('active', 'past_due')
            RETURNING id, strategy_id, status::text AS status""", t=now, t2=now, u=user_id)
        self._exec(conn, """
            UPDATE subscriptions
               SET pre_pause_status = 'reduce_only',
                   pre_pause_past_due_since = coalesce(pre_pause_past_due_since, CAST(:t AS timestamptz))
             WHERE user_id = CAST(:u AS uuid) AND status = 'paused_user'
               AND pre_pause_status IN ('active', 'past_due')
            RETURNING id""", t=now, u=user_id)
        return rows

    def subscribed_seconds(self, conn: Any, user_id: str, strategy_id: str, now: datetime) -> int:
        """F14: total time the user has held a subscription to the strategy (pending excluded; a cancelled or
        closing one counts until it ended)."""
        row = self._one(conn, """
            SELECT coalesce(sum(greatest(0, extract(epoch FROM (
                       CASE WHEN status = 'cancelled' THEN coalesce(cancelled_at, status_changed_at)
                            WHEN status = 'closing' THEN status_changed_at
                            ELSE CAST(:now AS timestamptz) END - created_at)))), 0)::bigint AS secs
              FROM subscriptions
             WHERE user_id = CAST(:u AS uuid) AND strategy_id = CAST(:s AS uuid) AND status <> 'pending'""",
                        now=now, u=user_id, s=strategy_id)
        return int(row["secs"]) if row else 0

    def accrued_profit_share_inputs(self, conn: Any, user_id: str) -> list[dict]:
        """M1: per subscription still accruing profit share — cum_pnl / hwm / pinned creator rate and the attributed
        PnL not yet settled (fills + funding after pnl_cursor; same sources as the settlement repo)."""
        return self._all(conn, """
            SELECT s.id, s.status::text AS status, s.cum_pnl_micro, s.hwm_micro, st.in_house,
                   coalesce(s.profit_share_bps, st.profit_share_bps, 0) AS profit_share_bps,
                   (SELECT coalesce(sum(coalesce(f.net_pnl_micro, f.closed_pnl_micro - f.fee_micro)), 0)::bigint
                      FROM fills f WHERE f.subscription_id = s.id
                       AND (s.pnl_cursor IS NULL OR f.time > s.pnl_cursor)) AS realized_micro,
                   (SELECT coalesce(sum(coalesce(e.attributed_micro, 0)), 0)::bigint
                      FROM funding_events e WHERE e.subscription_id = s.id
                       AND (s.pnl_cursor IS NULL OR e.time > s.pnl_cursor)) AS funding_micro
              FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
             WHERE s.user_id = CAST(:u AS uuid)
               AND (s.status IN ('active', 'past_due', 'reduce_only', 'paused_user', 'closing')
                    OR (s.status = 'cancelled' AND s.cancelled_at IS NOT NULL
                        AND (s.pnl_cursor IS NULL OR s.pnl_cursor <= s.cancelled_at)))""", u=user_id)

    # ------------------------------------------------------------------------------------ funding source (F4/H4)
    def card_unspent(self, conn: Any, user_id: str) -> int:
        row = self._one(conn, "SELECT fee_funding_card_unspent(CAST(:u AS uuid)) AS cu", u=user_id)
        return int(row["cu"]) if row and row["cu"] is not None else 0

    def payable_card_held(self, conn: Any, account_code: str, since: datetime) -> int:
        row = self._one(conn, "SELECT payable_card_held(CAST(:c AS text), CAST(:s AS timestamptz)) AS h",
                        c=account_code, s=since)
        return int(row["h"]) if row and row["h"] is not None else 0

    def usdc_credited_minus_withdrawn(self, conn: Any, user_id: str) -> int:
        row = self._one(conn, """
            SELECT (SELECT coalesce(sum(amount_micro), 0) FROM deposits
                     WHERE user_id = CAST(:u AS uuid) AND withdrawable AND status = 'credited')
                 - (SELECT coalesce(sum(amount_micro), 0) FROM withdrawals
                     WHERE beneficiary_user_id = CAST(:u2 AS uuid) AND status <> 'rejected') AS w""",
                        u=user_id, u2=user_id)
        return int(row["w"]) if row else 0

    # ------------------------------------------------------------------------------------ security holds (F5)
    def extend_security_hold(self, conn: Any, user_id: str, until: datetime) -> None:
        self._exec(conn, """
            UPDATE users SET security_hold_until = greatest(coalesce(security_hold_until, CAST(:t AS timestamptz)),
                                                          CAST(:t2 AS timestamptz))
             WHERE id = CAST(:u AS uuid) RETURNING id""", t=until, t2=until, u=user_id)

    def payout_context(self, conn: Any, user_id: str, to_address: str, since: datetime) -> dict:
        """What an approving admin must see: destination wallet age, the user's security hold and recent security
        events (audit_log)."""
        row = self._one(conn, """
            SELECT (SELECT w.verified_at FROM wallets w
                     WHERE w.master_address = :a AND w.user_id = CAST(:u AS uuid)) AS to_address_verified_at,
                   (SELECT u.security_hold_until FROM users u WHERE u.id = CAST(:u2 AS uuid)) AS security_hold_until""",
                        a=to_address, u=user_id, u2=user_id) or {}
        events = self._all(conn, """
            SELECT action, created_at FROM audit_log
             WHERE actor IN (CAST(:a1 AS text), CAST(:a2 AS text)) AND action = ANY(CAST(:acts AS text[]))
               AND created_at >= CAST(:since AS timestamptz)
             ORDER BY created_at DESC LIMIT 10""", a1=f"user:{user_id}", a2=f"admin:{user_id}",
                           acts=list(SECURITY_EVENT_ACTIONS), since=since)
        return {"to_address_verified_at": row.get("to_address_verified_at"),
                "security_hold_until": row.get("security_hold_until"), "events": events}

    # ------------------------------------------------------------------------------------ referrals (F7/M2)
    def record_ip_net(self, conn: Any, user_id: str, net_hash: str) -> bool:
        row = self._one(conn, """
            INSERT INTO user_ip_nets (user_id, net_hash) VALUES (CAST(:u AS uuid), :h)
            ON CONFLICT (user_id, net_hash) DO UPDATE SET last_seen = now()
            RETURNING (xmax = 0) AS inserted""", u=user_id, h=net_hash)
        return bool(row and row["inserted"])

    def set_device_fp_hash(self, conn: Any, user_id: str, device_hash: str) -> None:
        self._exec(conn, """UPDATE users SET device_fp_hash = :h WHERE id = CAST(:u AS uuid) AND device_fp_hash IS NULL
                            RETURNING id""", h=device_hash, u=user_id)

    def referral_identity(self, conn: Any, user_id: str, since: datetime) -> dict:
        """Wallets, device hashes and recent sign-in network hashes of a user (self-referral comparison)."""
        row = self._one(conn, """
            SELECT (SELECT coalesce(json_agg(w.master_address), '[]') FROM wallets w
                     WHERE w.user_id = CAST(:u AS uuid)) AS wallets,
                   (SELECT coalesce(json_agg(d.device_hash), '[]') FROM user_devices d
                     WHERE d.user_id = CAST(:u2 AS uuid)) AS devices,
                   (SELECT coalesce(json_agg(n.net_hash), '[]') FROM user_ip_nets n
                     WHERE n.user_id = CAST(:u3 AS uuid) AND n.last_seen >= CAST(:since AS timestamptz)) AS networks,
                   (SELECT u.device_fp_hash FROM users u WHERE u.id = CAST(:u4 AS uuid)) AS device_fp_hash""",
                        u=user_id, u2=user_id, u3=user_id, u4=user_id, since=since) or {}

        def _list(v: Any) -> list[str]:
            if isinstance(v, str):
                import json
                v = json.loads(v)
            return [str(x) for x in (v or []) if x]

        devices = _list(row.get("devices"))
        if row.get("device_fp_hash"):
            devices.append(str(row["device_fp_hash"]))
        return {"wallets": _list(row.get("wallets")), "devices": sorted(set(devices)),
                "networks": _list(row.get("networks"))}

    def flag_referral(self, conn: Any, user_id: str, reason: str, now: datetime) -> None:
        self._exec(conn, """UPDATE users SET referral_flagged_at = coalesce(referral_flagged_at, CAST(:t AS timestamptz)),
                                   referral_flag_reason = :r
                             WHERE id = CAST(:u AS uuid) RETURNING id""", t=now, r=reason[:200], u=user_id)

    # ------------------------------------------------------------------------------------ maker-checker (F6/F12)
    def cancel_pending_changes(self, conn: Any, target: str, reason: str, now: datetime,
                               kinds: tuple[str, ...] = ("strategy_list", "strategy_price")) -> list[dict]:
        """Pending proposals for a target become 'cancelled' (no checker: the state they were reviewed in is gone)."""
        return self._all(conn, """
            UPDATE admin_changes SET status = 'cancelled', decided_at = CAST(:t AS timestamptz), decision_reason = :r
             WHERE target = :tg AND status = 'pending' AND kind = ANY(CAST(:k AS text[]))
            RETURNING id, kind""", t=now, r=reason[:500], tg=target, k=list(kinds))

    # ------------------------------------------------------------------------------------ payouts (F9/M4)
    def set_payout_send(self, conn: Any, kind: str, payout_id: str, *, nonce: int, admin_id: str,
                        now: datetime) -> Optional[dict]:
        """Issue the usdSend nonce ONCE (approved_2 only). Returns {send_nonce, send_issued_at} of the row (the one
        already issued when there is one)."""
        table = "withdrawals" if kind == "withdrawal" else "payouts"
        sql = f"""UPDATE {table} SET send_nonce = :n, send_issued_at = CAST(:t AS timestamptz),
                         send_issued_by = CAST(:a AS uuid)
                   WHERE id = CAST(:id AS uuid) AND status = 'approved_2' AND send_nonce IS NULL
                  RETURNING send_nonce, send_issued_at"""
        row = self._one(conn, sql, n=int(nonce), t=now, a=admin_id, id=payout_id)
        if row is not None:
            return row
        return self._one(conn, f"""SELECT send_nonce, send_issued_at FROM {table}
                                   WHERE id = CAST(:id AS uuid) AND send_nonce IS NOT NULL""", id=payout_id)

    def payout_send_state(self, conn: Any, kind: str, payout_id: str) -> Optional[dict]:
        table = "withdrawals" if kind == "withdrawal" else "payouts"
        return self._one(conn, f"SELECT send_nonce, send_issued_at FROM {table} WHERE id = CAST(:id AS uuid)",
                         id=payout_id)

    def claim_payout_tx_hash(self, conn: Any, tx_hash: str, kind: str, payout_id: str) -> bool:
        """One on-chain transfer settles ONE payout/withdrawal (PK). False = hash already claimed."""
        row = self._one(conn, """INSERT INTO payout_tx_hashes (tx_hash, kind, payout_id)
                                 VALUES (lower(:h), :k, CAST(:id AS uuid))
                                 ON CONFLICT DO NOTHING RETURNING tx_hash""", h=tx_hash, k=kind, id=payout_id)
        return row is not None

    # ------------------------------------------------------------------------------------ deposits (L1)
    def mark_deposit_reversed_ref(self, conn: Any, payment_intent: str, user_id: str) -> int:
        return self._exec(conn, """UPDATE deposits SET status = 'reversed'
                                    WHERE external_ref = :r AND user_id = CAST(:u AS uuid) AND status = 'credited'
                                   RETURNING id""", r=payment_intent, u=user_id)
