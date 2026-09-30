"""Security-fix round — pure-Python checks (no database, no FastAPI). Each test reproduces the finding, then shows
the fix blocks it. docs/security/REVIEW_AUTH_API.md (F3 F5 F7 F8 F10 F18 F19) and REVIEW_MONEY.md (H3 M1 M8 L1)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.domain import billing  # noqa: E402
from app.domain import referrals as ref  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
USD = 1_000_000


# ------------------------------------------------------------------------------------------------ F3 / H3
class ResumeAfterPauseTest(unittest.TestCase):
    def test_review_poc_pause_unpause_no_longer_launders_reduce_only(self):
        # the review's PoC: reduce_only, pause, unpause → 'active' with entries allowed
        d = billing.resume_after_pause("reduce_only", NOW - timedelta(hours=100), balance_micro=-5 * USD,
                                       amount_due_micro=0, now=NOW)
        self.assertEqual(d.status, "reduce_only")
        self.assertFalse(billing.entries_allowed(d.status, d.past_due_since, NOW))

    def test_grace_clock_is_kept(self):
        since = NOW - timedelta(hours=10)
        d = billing.resume_after_pause("past_due", since, -1, 0, NOW)
        self.assertEqual((d.status, d.past_due_since), ("past_due", since))
        d = billing.resume_after_pause("past_due", NOW - timedelta(hours=73), -1, 0, NOW)
        self.assertEqual(d.status, "reduce_only")

    def test_renewal_due_while_paused_needs_the_money(self):
        d = billing.resume_after_pause("active", None, balance_micro=5 * USD, amount_due_micro=20 * USD, now=NOW)
        self.assertNotEqual(d.status, "active")          # caller refuses the unpause (402) instead of trading
        d = billing.resume_after_pause("reduce_only", NOW - timedelta(days=4), 30 * USD, 20 * USD, NOW)
        self.assertEqual((d.status, d.past_due_since), ("active", None))

    def test_legacy_rows_and_pending(self):
        self.assertEqual(billing.resume_after_pause(None, None, 0, 0, NOW).status, "active")
        self.assertEqual(billing.resume_after_pause(None, None, -1, 0, NOW).status, "past_due")
        self.assertEqual(billing.resume_after_pause("pending", None, -1, 0, NOW).status, "pending")
        with self.assertRaises(ValueError):
            billing.resume_after_pause("cancelled", None, 0, 0, NOW)


# ------------------------------------------------------------------------------------------------ F7 / M2
class SelfReferralTest(unittest.TestCase):
    def test_network_is_a_flag_device_and_wallet_block(self):
        a = ref.ReferralIdentity.of("r", wallets=["0xA"], devices=["d1"], networks=["n1"])
        same_net = ref.ReferralIdentity.of("e", devices=["d2"], networks=["n1"])
        same_dev = ref.ReferralIdentity.of("e", devices=["d1"], networks=["n9"])
        self.assertEqual(ref.self_referral_reasons(a, same_net), ("same_network",))
        self.assertFalse(ref.blocking(ref.self_referral_reasons(a, same_net)))
        self.assertTrue(ref.blocking(ref.self_referral_reasons(a, same_dev)))
        self.assertEqual(ref.self_referral_reasons(a, ref.ReferralIdentity.of("e", networks=["", None])), ())

    def test_referral_guard_uses_request_hashes_for_a_new_account(self):
        from app.api.referral_guard import self_referral_check

        class Store:
            def referral_identity(self, conn, uid, since):
                return {"wallets": [], "devices": ["dev-ref"], "networks": ["net-ref"]}

        svc = SimpleNamespace(store=Store(), now=lambda: NOW)
        blocked, reasons = self_referral_check(None, svc, referrer={"id": "r1"}, referee={"id": None},
                                               referee_devices=["dev-ref"], referee_networks=[None])
        self.assertEqual((blocked, reasons), (True, ("same_device",)))
        blocked, reasons = self_referral_check(None, svc, referrer={"id": "r1"}, referee={"id": None},
                                               referee_devices=["other"], referee_networks=["net-ref"])
        self.assertEqual((blocked, reasons), (False, ("same_network",)))


# ------------------------------------------------------------------------------------------------ F5 / F7 sign-in
class SignInSecurityTest(unittest.TestCase):
    def test_network_hash_groups_a_24_and_a_64(self):
        from app.api.login_events import network_hash
        p = b"p" * 32
        self.assertEqual(network_hash("203.0.113.5", p), network_hash("203.0.113.250", p))
        self.assertNotEqual(network_hash("203.0.113.5", p), network_hash("203.0.114.5", p))
        self.assertEqual(network_hash("2001:db8:1:2::1", p), network_hash("2001:db8:1:2:ffff::9", p))
        self.assertNotEqual(network_hash("2001:db8:1:2::1", p), network_hash("2001:db8:1:3::1", p))
        self.assertEqual(network_hash("::ffff:203.0.113.5", p), network_hash("203.0.113.7", p))
        self.assertIsNone(network_hash("not-an-ip", p))
        self.assertIsNone(network_hash(None, p))

    def test_new_device_and_mfa_change_put_a_48h_hold(self):
        from app.api import login_events as le
        holds: dict[str, datetime] = {}
        seen: set = set()

        class Store:
            def record_login_country(self, conn, uid, c):
                return False

            def record_device(self, conn, uid, h, label):
                new = (uid, h) not in seen
                had = any(u == uid for u, _ in seen)
                seen.add((uid, h))
                return new and had

            def set_mfa_factor_hash(self, conn, uid, fh):
                pass

            def extend_security_hold(self, conn, uid, until):
                holds[uid] = max(holds.get(uid, until), until)

        svc = SimpleNamespace(store=Store(), config=SimpleNamespace(pepper=b"p" * 32), now=lambda: NOW,
                              notifier=SimpleNamespace(notify=lambda conn, **kw: None),
                              audit=SimpleNamespace(write=lambda conn, **kw: None))
        user = {"id": "u1", "mfa_factor_hash": None}
        le.record_sign_in(None, svc, user=user, claims={}, country=None, device_hash="a" * 64, device_label_=None,
                          ip_hash=None)
        self.assertEqual(holds, {})                                  # first device: silent, no hold
        le.record_sign_in(None, svc, user=user, claims={}, country=None, device_hash="b" * 64, device_label_=None,
                          ip_hash=None)
        self.assertEqual(holds["u1"], NOW + timedelta(hours=48))
        holds.clear()
        claims = {"firebase": {"second_factor_identifier": "factor-2"}}
        le.record_sign_in(None, svc, user={"id": "u1", "mfa_factor_hash": "0" * 64}, claims=claims, country=None,
                          device_hash=None, device_label_=None, ip_hash=None)
        self.assertEqual(holds["u1"], NOW + timedelta(hours=48))


# ------------------------------------------------------------------------------------------------ F18
class DisplayNameTest(unittest.TestCase):
    def test_provider_name_is_cleaned(self):
        from app.api.validation import clean_display_name
        self.assertEqual(clean_display_name("  Ali‮<script>x</script>\n  Baba "), "Aliscriptx/script Baba")
        self.assertEqual(clean_display_name("x" * 200), "x" * 64)
        self.assertIsNone(clean_display_name("​​"))
        self.assertIsNone(clean_display_name(None))


# ------------------------------------------------------------------------------------------------ F10 / F19
class CachesTest(unittest.TestCase):
    def test_verified_token_cache_ttl_and_exp(self):
        from app.api.caches import VerifiedTokenCache
        t = [1000.0]
        c = VerifiedTokenCache(ttl=60, clock=lambda: t[0])
        c.put("tok", "ctx", exp=1030)                               # expires with the token, before the TTL
        self.assertEqual(c.get("tok"), "ctx")
        t[0] = 1031
        self.assertIsNone(c.get("tok"))
        c.put("tok2", "ctx2", exp=10_000)
        t[0] = 1031 + 59
        self.assertEqual(c.get("tok2"), "ctx2")
        t[0] = 1031 + 61
        self.assertIsNone(c.get("tok2"))                            # revocation re-checked after ≤ 60 s
        c.put("old", "x", exp=10)                                   # already expired: never cached
        self.assertIsNone(c.get("old"))

    def test_leaderboard_cache_computes_once_per_ttl(self):
        from app.api.caches import TtlCache
        calls = []
        c = TtlCache(60)
        for _ in range(50):
            c.get_or_compute(("svc", "roi", "30d"), lambda: calls.append(1) or len(calls))
        self.assertEqual(len(calls), 1)
        c.get_or_compute(("svc", "pnl", "30d"), lambda: calls.append(1))
        self.assertEqual(len(calls), 2)


# ------------------------------------------------------------------------------------------------ F8
class UsdcVerifiedAtTest(unittest.TestCase):
    TREASURY = "0x" + "7" * 40
    SENDER = "0x" + "1" * 40

    def det(self, time_ms: int):
        return {"tx_hash": "0x" + "ab" * 32, "from_address": self.SENDER, "to_address": self.TREASURY,
                "amount_micro": 50 * USD, "token": "USDC", "time_ms": time_ms}

    def test_transfer_before_wallet_verification_is_held(self):
        from app.payments.usdc import credit_from_detection
        verified = NOW
        ms = int(NOW.timestamp() * 1000)
        # before: any historical transfer from the address was credited to whoever verified it later
        old = credit_from_detection(self.det(ms - 86_400_000), treasury_address=self.TREASURY,
                                    user_for_address=lambda a: "u1")
        self.assertIsNotNone(old.credit)
        held = credit_from_detection(self.det(ms - 86_400_000), treasury_address=self.TREASURY,
                                     user_for_address=lambda a: "u1", verified_at_for_address=lambda a: verified)
        self.assertIsNone(held.credit)
        self.assertIn("predates", held.held)
        ok = credit_from_detection(self.det(ms + 5_000), treasury_address=self.TREASURY,
                                   user_for_address=lambda a: "u1",
                                   verified_at_for_address=lambda a: verified.isoformat())
        self.assertIsNotNone(ok.credit)


# ------------------------------------------------------------------------------------------------ L1
class FullReversalMetaTest(unittest.TestCase):
    def test_dispute_and_full_refund_are_tagged(self):
        from test_payments_stripe_events import CFG, DepositRecord, USER, charge, dispute, event, handle_event
        rec = DepositRecord(USER, "pi_123", "usd", 5000, 50 * USD)
        out = handle_event(event("charge.dispute.created", dispute()), config=CFG, lookup=lambda p: rec)
        self.assertTrue(out.debits[0].meta["full_reversal"])
        part = handle_event(event("charge.refunded", charge(refunded=2000), prev={"amount_refunded": 0}), config=CFG)
        self.assertFalse(part.debits[0].meta["full_reversal"])
        full = handle_event(event("charge.refunded", charge(refunded=5000), prev={"amount_refunded": 2000}), config=CFG)
        self.assertTrue(full.debits[0].meta["full_reversal"])


# ------------------------------------------------------------------------------------------------ M1 / H3 with fakes
class BillingOpsFakeTest(unittest.TestCase):
    def world(self):
        from app.api.testing_security import FakeSecurityStoreMixin
        from app.config import get_settings

        w = SimpleNamespace(now=NOW, subscriptions={}, strategies={}, users={"u1": {"id": "u1"}}, wallets={},
                            audit=[], devices=set(), changes={}, withdrawals={}, payouts={}, deposits={})
        bal = {"user:u1:fee_balance": 0}

        class Store(FakeSecurityStoreMixin):
            def __init__(self):
                self.w = w

            def get_strategy(self, conn, sid, for_update=False):
                return dict(w.strategies[sid])

        posted = []

        class Ledger:
            def ensure_account(self, conn, code):
                pass

            def post(self, conn, *, idempotency_key, kind, memo, entries, created_by):
                posted.append((idempotency_key, kind, entries))
                for c, a in entries:
                    bal[c] = bal.get(c, 0) + a
                return idempotency_key

            def balance(self, conn, code):
                return bal.get(code, 0)

        class Domain:
            def subscription_split(self, price):
                return price * 97 // 100, price - price * 97 // 100

        svc = SimpleNamespace(store=Store(), ledger=Ledger(), domain=Domain(), settings=get_settings(),
                              notifier=SimpleNamespace(notify=lambda conn, **kw: None), now=lambda: NOW)
        w.strategies["s1"] = {"id": "s1", "status": "listed", "price_monthly_micro": 20 * USD,
                              "owner_user_id": "c1", "in_house": False, "profit_share_bps": 1000}
        w.subscriptions["sub1"] = {"id": "sub1", "user_id": "u1", "strategy_id": "s1", "status": "active",
                                   "created_at": NOW - timedelta(days=45), "current_period_end": NOW - timedelta(days=15),
                                   "past_due_since": None, "price_monthly_micro": 10 * USD, "profit_share_bps": 1000,
                                   "cum_pnl_micro": 0, "hwm_micro": 0}
        return w, bal, posted, svc

    def test_unpause_charges_the_pinned_price_once_with_the_settlement_key(self):
        from app.api import billing_ops
        from app.errors import InsufficientBalance
        w, bal, posted, svc = self.world()
        svc.store.pause_subscription(None, "sub1")
        with self.assertRaises(InsufficientBalance):
            billing_ops.resume_subscription(None, svc, user_id="u1", sub_id="sub1", actor="user:u1")
        bal["user:u1:fee_balance"] = -12 * USD                       # tops up 12
        out = billing_ops.resume_subscription(None, svc, user_id="u1", sub_id="sub1", actor="user:u1")
        self.assertEqual((out["status"], out["charged_micro"]), ("active", 10 * USD))   # pinned 10, not the new 20
        from app.execution.settlement import renewal_key
        period_end = NOW - timedelta(days=15)
        self.assertEqual(posted[0][0], renewal_key("sub1", period_end))
        self.assertEqual(posted[0][0], billing_ops.renewal_key("sub1", period_end))
        self.assertEqual(w.subscriptions["sub1"]["current_period_end"], billing.next_renewal_after(
            NOW - timedelta(days=45), NOW))

    def test_accrued_profit_share_and_reserve_block_the_withdrawal(self):
        from app.api import billing_ops
        from app.errors import InsufficientBalance
        w, bal, posted, svc = self.world()
        w.subscriptions["sub1"].update(current_period_end=NOW + timedelta(days=10), unsettled_pnl_micro=20_000 * USD)
        bal["user:u1:fee_balance"] = -2_000 * USD
        self.assertEqual(billing_ops.accrued_profit_share(None, svc, "u1"), 2_300 * USD)   # 11.5 % of 20k
        with self.assertRaises(InsufficientBalance) as cm:
            billing_ops.require_withdrawal_headroom(None, svc, user_id="u1", amount_micro=2_000 * USD)
        self.assertEqual(cm.exception.details["max_withdrawal_micro"], 0)


# ------------------------------------------------------------------------------------------------ M8 (settlement)
class RenewalAfterCancelRaceTest(unittest.TestCase):
    def test_stale_snapshot_is_not_charged(self):
        from test_execution_settlement import Env, NOW as SNOW, sub, usd
        e = Env()
        period_end = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        e.repo.subs["sub1"] = sub(price_monthly_micro=usd(10), current_period_end=period_end)
        e.ledger.top_up("user1", usd(50))
        # the user cancelled after the settlement loaded its list: the row lock shows it
        e.repo.lock_for_billing = lambda sid: {"status": "cancelled", "cancelled_at": SNOW, "current_period_end": period_end}
        r = e.run(SNOW)
        self.assertFalse(any(k.startswith("sub:") for k in e.ledger.txs))
        self.assertEqual(r.renewals_skipped_stale, 1)
        e.repo.lock_for_billing = lambda sid: {"status": "active", "cancelled_at": None, "current_period_end": period_end}
        e.run(SNOW + timedelta(minutes=5))
        self.assertTrue(any(k.startswith("sub:") for k in e.ledger.txs))


if __name__ == "__main__":
    unittest.main()
