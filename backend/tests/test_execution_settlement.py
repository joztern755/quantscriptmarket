"""Daily settlement tests: profit share (exact sums, HWM, idempotency), renewals + status machine, plans,
builder-fee revenue recognition. Uses the real domain adapters from app.execution.wiring."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import (  # noqa: E402
    FakeAlerts,
    FakeClock,
    FakeLedger,
    FakeReferrals,
    FakeSettlementRepo,
    FakeUow,
    usd,
)

from app.execution.ports import BuilderFeeFill, LedgerLine, PlanAccount, SettlementSubscription  # noqa: E402
from app.execution.settlement import Settlement, profit_share_key  # noqa: E402
from app.execution.wiring import DomainBilling, DomainFees, DomainProfitShare  # noqa: E402

UTC = timezone.utc
D1 = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
CREATED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def sub(**kw) -> SettlementSubscription:
    base = dict(id="sub1", user_id="user1", strategy_id="s1", creator_user_id="creator1", in_house=False,
                status="active", profit_share_bps=1000, price_monthly_micro=0, cum_pnl_micro=0, hwm_micro=0,
                pnl_cursor=None, current_period_end=datetime(2026, 11, 1, 12, 0, tzinfo=UTC), past_due_since=None,
                created_at=CREATED)
    base.update(kw)
    return SettlementSubscription(**base)


class Env:
    def __init__(self, referrals=None):
        self.repo = FakeSettlementRepo()
        self.ledger = FakeLedger()
        self.uow = FakeUow()
        self.alerts = FakeAlerts()
        self.clock = FakeClock(NOW)
        self.s = Settlement(repo=self.repo, ledger=self.ledger, uow=self.uow, profit_share=DomainProfitShare(),
                            fees=DomainFees(), billing=DomainBilling(72), referrals=FakeReferrals(referrals),
                            alerts=self.alerts, clock=self.clock, builder_page_size=2)

    def run(self, now: datetime, settle_date: date | None = None):
        self.clock.set(now)
        return self.s.settle_daily(settle_date, now)

    def assert_ledger_balanced(self, tc: unittest.TestCase):
        tc.assertEqual(sum(self.ledger.balances.values()), 0)
        for _k, (_id, _kind, lines) in self.ledger.txs.items():
            tc.assertEqual(sum(ln.amount_micro for ln in lines), 0)


class ProfitShareSettlementTest(unittest.TestCase):
    def test_exact_sums_and_accounts(self):
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(1000))
        r = e.run(NOW)
        self.assertEqual(r.errors, [])
        _tx, kind, lines = e.ledger.txs[profit_share_key("sub1", D1)]
        self.assertEqual(kind, "profit_share")
        self.assertEqual(set(lines), {LedgerLine("user:user1:fee_balance", usd(115)),
                                      LedgerLine("creator:creator1:payable", -usd(100)),
                                      LedgerLine("platform:revenue:profit_share", -usd(15))})
        self.assertEqual(e.ledger.available("user1"), usd(385))
        s = e.repo.subs["sub1"]
        self.assertEqual((s.cum_pnl_micro, s.hwm_micro), (usd(1000), usd(1000)))
        self.assertEqual(s.pnl_cursor, datetime(2026, 10, 2, tzinfo=UTC))
        e.assert_ledger_balanced(self)

    def test_floor_rounding_remainder_to_platform(self):
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(1000))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), 333_333_333)
        e.run(NOW)
        lines = {ln.account_code: ln.amount_micro for ln in e.ledger.txs[profit_share_key("sub1", D1)][2]}
        self.assertEqual(lines["user:user1:fee_balance"], 38_333_333)       # floor(333_333_333 × 11.5%)
        self.assertEqual(lines["creator:creator1:payable"], -33_333_333)
        self.assertEqual(lines["platform:revenue:profit_share"], -5_000_000)

    def test_idempotent_rerun_same_date(self):
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), usd(1000))
        e.run(NOW)
        snapshot = dict(e.ledger.balances)
        ntx = len(e.ledger.txs)
        r2 = e.run(NOW + timedelta(minutes=5))
        self.assertEqual(r2.profit_share_skipped, 1)
        self.assertEqual(len(e.ledger.txs), ntx)
        self.assertEqual(e.ledger.balances, snapshot)

    def test_idempotent_even_if_settlement_row_was_lost(self):
        # ledger key already exists (e.g. the row write was retried): post returns the same tx, no double charge
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), usd(1000))
        e.run(NOW)
        e.repo.settled.clear()
        e.repo.subs["sub1"] = sub()   # simulate the subscription update having been rolled back
        e.run(NOW)
        self.assertEqual(e.ledger.available("user1"), usd(385))

    def test_losses_must_be_recovered_before_new_charge(self):
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(1000))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), usd(1000))
        e.run(NOW)                                                            # charge 115
        e.repo.add_pnl("sub1", datetime(2026, 10, 2, 6, tzinfo=UTC), -usd(500))
        e.run(NOW + timedelta(days=1))                                        # cum 500, no charge
        e.repo.add_pnl("sub1", datetime(2026, 10, 3, 6, tzinfo=UTC), usd(300))
        e.run(NOW + timedelta(days=2))                                        # cum 800 < hwm, no charge
        e.repo.add_pnl("sub1", datetime(2026, 10, 4, 6, tzinfo=UTC), usd(400))
        e.run(NOW + timedelta(days=3))                                        # cum 1200 → profit 200 → 23
        self.assertEqual(e.ledger.available("user1"), usd(1000) - usd(115) - usd(23))
        ps = [k for k in e.ledger.txs if k.startswith("ps:")]
        self.assertEqual(ps, ["ps:sub1:2026-10-02", "ps:sub1:2026-10-05"])
        s = e.repo.subs["sub1"]
        self.assertEqual((s.cum_pnl_micro, s.hwm_micro), (usd(1200), usd(1200)))
        e.assert_ledger_balanced(self)

    def test_in_house_all_to_platform(self):
        e = Env()
        e.repo.subs["sub1"] = sub(in_house=True, creator_user_id=None)
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), usd(1000))
        e.run(NOW)
        lines = {ln.account_code: ln.amount_micro for ln in e.ledger.txs[profit_share_key("sub1", D1)][2]}
        self.assertEqual(lines, {"user:user1:fee_balance": usd(115), "platform:revenue:profit_share": -usd(115)})

    def test_charge_driving_balance_negative_moves_to_past_due(self):
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(10))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), usd(1000))
        r = e.run(NOW)
        self.assertEqual(e.ledger.available("user1"), -usd(105))
        self.assertEqual(e.repo.subs["sub1"].status, "past_due")
        self.assertEqual(e.repo.subs["sub1"].past_due_since, NOW)
        self.assertIn(("sub1", "active", "past_due"), r.status_changes)

    def test_one_bad_subscription_does_not_block_others(self):
        e = Env()
        e.repo.subs["bad"] = sub(id="bad", user_id="u2", creator_user_id=None)   # third-party without creator
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, tzinfo=UTC), usd(1000))
        r = e.run(NOW)
        self.assertEqual(len(r.errors), 1)
        self.assertIn(profit_share_key("sub1", D1), e.ledger.txs)


class RenewalStatusTest(unittest.TestCase):
    def test_past_due_then_reduce_only_then_paid(self):
        e = Env()
        period_end = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        e.repo.subs["sub1"] = sub(price_monthly_micro=9_990_000, current_period_end=period_end)
        e.ledger.top_up("user1", usd(5))
        e.run(NOW)
        s = e.repo.subs["sub1"]
        self.assertEqual((s.status, s.past_due_since, s.current_period_end), ("past_due", NOW, period_end))
        self.assertFalse(any(k.startswith("sub:") for k in e.ledger.txs))
        self.assertIn("subscription_past_due", e.alerts.kinds())

        e.run(NOW + timedelta(hours=48))
        self.assertEqual(e.repo.subs["sub1"].status, "past_due")               # still inside grace
        e.run(NOW + timedelta(hours=72))
        self.assertEqual(e.repo.subs["sub1"].status, "reduce_only")
        self.assertIn("subscription_reduce_only", e.alerts.kinds())

        e.ledger.top_up("user1", usd(20))
        later = NOW + timedelta(days=4)
        r = e.run(later)
        s = e.repo.subs["sub1"]
        self.assertEqual((s.status, s.past_due_since), ("active", None))
        self.assertEqual(s.current_period_end, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
        _tx, kind, lines = e.ledger.txs["sub:sub1:2026-10-01"]
        self.assertEqual(set(lines), {LedgerLine("user:user1:fee_balance", 9_990_000),
                                      LedgerLine("creator:creator1:payable", -9_690_300),       # 97%
                                      LedgerLine("platform:revenue:subscription", -299_700)})   # 3%
        self.assertEqual(r.renewals_charged, 1)
        self.assertEqual(e.ledger.available("user1"), usd(25) - 9_990_000)
        e.run(later + timedelta(minutes=1))
        self.assertEqual(sum(1 for k in e.ledger.txs if k.startswith("sub:")), 1)   # no double renewal
        e.assert_ledger_balanced(self)

    def test_on_time_renewal_advances_one_month(self):
        e = Env()
        e.repo.subs["sub1"] = sub(price_monthly_micro=usd(30), current_period_end=datetime(2026, 10, 1, 12, tzinfo=UTC),
                                  in_house=True, creator_user_id=None)
        e.ledger.top_up("user1", usd(100))
        e.run(datetime(2026, 10, 1, 12, 30, tzinfo=UTC), settle_date=date(2026, 10, 1))
        self.assertEqual(e.repo.subs["sub1"].current_period_end, datetime(2026, 11, 1, 12, tzinfo=UTC))
        lines = e.ledger.txs["sub:sub1:2026-10-01"][2]
        self.assertEqual(set(lines), {LedgerLine("user:user1:fee_balance", usd(30)),
                                      LedgerLine("platform:revenue:subscription", -usd(30))})

    def test_top_up_restores_active_without_renewal_due(self):
        e = Env()
        e.repo.subs["sub1"] = sub(status="past_due", past_due_since=NOW - timedelta(hours=10))
        e.ledger.top_up("user1", usd(1))
        e.run(NOW)
        self.assertEqual(e.repo.subs["sub1"].status, "active")

    def test_paused_and_cancelled_are_not_billed(self):
        e = Env()
        e.repo.subs["sub1"] = sub(status="paused_user", price_monthly_micro=usd(10),
                                  current_period_end=datetime(2026, 10, 1, tzinfo=UTC))
        e.run(NOW)
        self.assertEqual(e.repo.subs["sub1"].status, "paused_user")
        self.assertFalse(any(k.startswith("sub:") for k in e.ledger.txs))


class PlanRenewalTest(unittest.TestCase):
    def test_plan_renewed_or_past_due_then_downgraded(self):
        e = Env()
        pe = datetime(2026, 10, 1, 9, tzinfo=UTC)
        e.repo.plans["user1"] = PlanAccount("user1", "pro", usd(20), pe, None, anchor=datetime(2026, 9, 1, 9, tzinfo=UTC))
        e.repo.plans["user2"] = PlanAccount("user2", "max", usd(50), pe, None)
        e.ledger.top_up("user1", usd(50))
        e.run(NOW)
        self.assertEqual(e.repo.plans["user1"].plan_period_end, datetime(2026, 11, 1, 9, tzinfo=UTC))
        self.assertEqual(set(e.ledger.txs["plan:user1:2026-10-01"][2]),
                         {LedgerLine("user:user1:fee_balance", usd(20)), LedgerLine("platform:revenue:plans", -usd(20))})
        self.assertEqual(e.repo.plans["user2"].past_due_since, NOW)
        e.run(NOW + timedelta(hours=71))
        self.assertEqual(e.repo.plans["user2"].plan, "max")
        e.run(NOW + timedelta(hours=72))
        self.assertEqual(e.repo.plans["user2"].plan, "free")
        self.assertIn("plan_downgraded", e.alerts.kinds())
        e.assert_ledger_balanced(self)


A1, A2, A3 = ("0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20)


class BuilderFeeRecognitionTest(unittest.TestCase):
    def test_split_per_fill_with_referral_and_idempotent(self):
        e = Env(referrals={"user1": ("ref1", 5000)})
        t = datetime(2026, 10, 1, 8, tzinfo=UTC)
        e.repo.fills = {
            "t1": BuilderFeeFill("t1", "sub1", "user1", "creator1", False, 1000, t, A1),
            "t2": BuilderFeeFill("t2", "sub2", "user2", None, True, 999, t, A2),
            "t3": BuilderFeeFill("t3", None, None, None, False, 500, t, A3),
            "t4": BuilderFeeFill("t4", "sub1", "user1", "creator1", False, 10, datetime(2026, 10, 2, 1, tzinfo=UTC), A1),
            # the same trade (tid) seen from the counterparty's account (another of our users) is a distinct fill
            "t1b": BuilderFeeFill("t1", "sub2", "user2", None, True, 1000, t, A2),
        }
        r = e.run(NOW)
        self.assertEqual(r.builder_fills_recognised, 4)                     # t4 is after the cut-off
        self.assertEqual(set(e.ledger.txs[f"bf:{A2}:t1"][2]), {
            LedgerLine("builder:hl_receivable", 1000), LedgerLine("platform:revenue:builder", -1000)})
        self.assertEqual(set(e.ledger.txs[f"bf:{A1}:t1"][2]), {
            LedgerLine("builder:hl_receivable", 1000), LedgerLine("creator:creator1:payable", -500),
            LedgerLine("referrer:ref1:payable", -100), LedgerLine("platform:revenue:builder", -400)})
        self.assertEqual(set(e.ledger.txs[f"bf:{A2}:t2"][2]), {
            LedgerLine("builder:hl_receivable", 999), LedgerLine("platform:revenue:builder", -999)})
        self.assertEqual(set(e.ledger.txs[f"bf:{A3}:t3"][2]), {
            LedgerLine("builder:hl_receivable", 500), LedgerLine("platform:revenue:builder", -500)})
        self.assertIn("builder_fee_unattributed_fill", e.alerts.kinds())
        self.assertEqual(r.builder_fees_recognised_micro, 3499)
        n = len(e.ledger.txs)
        e.run(NOW + timedelta(minutes=1))
        self.assertEqual(len(e.ledger.txs), n)
        e.assert_ledger_balanced(self)


if __name__ == "__main__":
    unittest.main()
