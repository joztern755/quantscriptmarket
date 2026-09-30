"""Daily settlement tests: profit share (exact sums, HWM, idempotency), renewals + status machine, plans,
builder-fee revenue recognition. Uses the real domain adapters from app.execution.wiring."""
from __future__ import annotations

import sys
from dataclasses import replace
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

from app.execution.ports import BuilderFeeFill, DataCoverage, LedgerLine, PlanAccount, SettlementSubscription  # noqa: E402
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
                created_at=CREATED, trading_address="0x" + "ab" * 20)
    base.update(kw)
    return SettlementSubscription(**base)


class FakeEvents:
    """UserEventSink: records events (dedup like events_outbox) and the low-balance hook calls; the hook applies the
    real threshold logic (app.alerts.delivery.low_balance_alerts) against a fixed monthly need."""

    def __init__(self, need_micro: int = 0) -> None:
        self.events: dict[str, dict] = {}
        self.balance_calls: list[tuple[str, int, int]] = []
        self.low: list[str] = []
        self.need = need_micro

    def emit(self, *, user_id, kind, severity, payload, dedup_key):
        if dedup_key in self.events:
            return False
        self.events[dedup_key] = {"user_id": user_id, "kind": kind, "severity": severity, "payload": dict(payload)}
        return True

    def fee_balance_changed(self, *, user_id, prev_micro, new_micro, now):
        from app.alerts.delivery import low_balance_alerts

        self.balance_calls.append((user_id, prev_micro, new_micro))
        kinds = [a["kind"] for a in low_balance_alerts(user_id, prev_micro, new_micro, self.need)]
        self.low += kinds
        return kinds

    def of(self, kind):
        return [e for e in self.events.values() if e["kind"] == kind]


class Env:
    def __init__(self, referrals=None, events=None):
        self.repo = FakeSettlementRepo()
        self.ledger = FakeLedger()
        self.uow = FakeUow()
        self.alerts = FakeAlerts()
        self.clock = FakeClock(NOW)
        self.s = Settlement(repo=self.repo, ledger=self.ledger, uow=self.uow, profit_share=DomainProfitShare(),
                            fees=DomainFees(), billing=DomainBilling(72), referrals=FakeReferrals(referrals),
                            alerts=self.alerts, clock=self.clock, builder_page_size=2, events=events)
        self.events = events

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


class UserEventsTest(unittest.TestCase):
    """profit_share_charged per charge, subscription_renewed, and the low-balance hook after every fee-balance
    posting (profit share, renewal, plan) — each exactly once across idempotent re-runs."""

    def test_profit_share_charged_and_low_balance_once(self):
        ev = FakeEvents(need_micro=usd(200))
        e = Env(events=ev)
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(150))                      # 75 % of the need
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(1000))
        r = e.run(NOW)
        self.assertEqual(r.errors, [])
        charged = ev.of("profit_share_charged")
        self.assertEqual(len(charged), 1)
        p = charged[0]["payload"]
        self.assertEqual((charged[0]["user_id"], p["amount_micro"], p["profit_micro"], p["rate_bps"]),
                         ("user1", usd(115), usd(1000), 1150))
        self.assertEqual((p["creator_micro"], p["platform_micro"], p["strategy_id"]), (usd(100), usd(15), "s1"))
        self.assertEqual((p["period_start"], p["period_end"]),
                         (CREATED.isoformat(), datetime(2026, 10, 2, tzinfo=UTC).isoformat()))
        self.assertEqual(ev.balance_calls, [("user1", usd(150), usd(35))])
        self.assertEqual(ev.low, ["balance_low", "balance_low"])  # 75 % → 17.5 %: one alert per threshold (50, 20)
        # re-run: nothing new (idempotent ledger key → no hook, no event)
        e.repo.settled.clear()
        e.repo.subs["sub1"] = sub()
        e.run(NOW + timedelta(minutes=5))
        self.assertEqual(len(ev.of("profit_share_charged")), 1)
        self.assertEqual(len(ev.balance_calls), 1)

    def test_renewal_and_plan_call_hook_and_emit_renewed(self):
        ev = FakeEvents(need_micro=usd(100))
        e = Env(events=ev)
        due = datetime(2026, 10, 1, 12, tzinfo=UTC)
        e.repo.subs["sub1"] = sub(price_monthly_micro=usd(30), current_period_end=due)
        e.repo.plans["user1"] = PlanAccount(user_id="user1", plan="pro", price_monthly_micro=usd(20),
                                            plan_period_end=due, past_due_since=None)
        e.ledger.top_up("user1", usd(60))
        r = e.run(NOW)
        self.assertEqual((r.renewals_charged, r.plans_renewed), (1, 1), r)
        self.assertEqual(ev.balance_calls, [("user1", usd(60), usd(30)), ("user1", usd(30), usd(10))])
        self.assertEqual(ev.low, ["balance_low", "balance_low"])   # 60 → 30 % crosses 50 %; 30 → 10 % crosses 20 %
        ren = ev.of("subscription_renewed")
        self.assertEqual(len(ren), 1)
        self.assertEqual(ren[0]["payload"]["amount_micro"], usd(30))
        e.run(NOW + timedelta(minutes=5))
        self.assertEqual(len(ev.balance_calls), 2)

    def test_event_sink_failure_never_blocks_settlement(self):
        class Broken:
            def emit(self, **kw):
                raise RuntimeError("down")

            def fee_balance_changed(self, **kw):
                raise RuntimeError("down")

        e = Env(events=Broken())
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(1000))
        r = e.run(NOW)
        self.assertEqual((r.errors, r.profit_share_charged_micro), ([], usd(115)))


class DataCoverageGuardTest(unittest.TestCase):
    """settle-daily must not settle a (subscription, day) before fills-ingest AND funding-scan have synced the
    subscription's trading address past the cut-off (+ margin): deferred → no ledger post, no cursor move, no renewal,
    one ``settlement_deferred`` ops event per date; the next run (retry slot) settles it."""

    ADDR = "0x" + "ab" * 20
    CUT = datetime(2026, 10, 2, tzinfo=UTC)

    def ms(self, dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def env(self, events=True):
        from app.execution.ports import DataCoverage

        e = Env(events=FakeEvents() if events else None)
        e.repo.coverage_default = None                                   # nothing synced unless a test says so
        e.cov = lambda fills, funding: e.repo.coverage.__setitem__(
            self.ADDR, DataCoverage(fills_ms=None if fills is None else self.ms(fills),
                                    funding_ms=None if funding is None else self.ms(funding)))
        e.repo.subs["sub1"] = sub(price_monthly_micro=usd(10), current_period_end=datetime(2026, 10, 1, 12, tzinfo=UTC))
        e.ledger.top_up("user1", usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(1000))
        return e

    def test_fills_behind_defers_everything_then_retry_settles(self):
        e = self.env()
        e.cov(self.CUT - timedelta(minutes=5), self.CUT + timedelta(minutes=7))
        before = dict(e.ledger.balances)
        r = e.run(NOW)
        self.assertEqual((r.profit_share_deferred, r.profit_share_settled, r.renewals_charged), (1, 0, 0))
        self.assertEqual(r.deferred[0]["missing"], ["fills_ingest"])
        self.assertEqual(r.deferred[0]["needed_until"], self.CUT.isoformat())
        self.assertNotIn(profit_share_key("sub1", D1), e.ledger.txs)
        self.assertFalse(any(k.startswith("sub:") for k in e.ledger.txs))      # renewal not charged on top
        self.assertEqual(e.ledger.balances, before)
        self.assertIsNone(e.repo.subs["sub1"].pnl_cursor)
        self.assertEqual(e.repo.subs["sub1"].status, "active")
        ev = e.events.of("settlement_deferred")
        self.assertEqual(len(ev), 1)
        self.assertIsNone(ev[0]["user_id"])                                 # ops event
        self.assertEqual((ev[0]["severity"], ev[0]["payload"]["deferred"], ev[0]["payload"]["subscriptions"]),
                         ("warn", 1, ["sub1"]))
        self.assertIn(f"settlement_deferred:{D1.isoformat()}", e.events.events)
        # 02:30 retry: still behind → still deferred, NO second event for the same date
        r = e.run(NOW + timedelta(hours=2))
        self.assertEqual(r.profit_share_deferred, 1)
        self.assertEqual(len(e.events.of("settlement_deferred")), 1)
        # fills-ingest caught up → 06:30 retry settles day + renewal exactly once
        e.cov(self.CUT + timedelta(minutes=25), self.CUT + timedelta(minutes=7))
        r = e.run(NOW + timedelta(hours=6))
        self.assertEqual((r.profit_share_deferred, r.profit_share_settled, r.renewals_charged), (0, 1, 1))
        self.assertIn(profit_share_key("sub1", D1), e.ledger.txs)
        self.assertEqual(e.repo.subs["sub1"].pnl_cursor, self.CUT)
        self.assertEqual(r.errors, [])
        e.assert_ledger_balanced(self)

    def test_funding_behind_defers(self):
        e = self.env()
        e.cov(self.CUT + timedelta(minutes=25), self.CUT - timedelta(minutes=53))
        r = e.run(NOW)
        self.assertEqual([d["missing"] for d in r.deferred], [["funding_scan"]])
        self.assertNotIn(profit_share_key("sub1", D1), e.ledger.txs)

    def test_never_synced_or_no_address_defers(self):
        e = self.env()
        r = e.run(NOW)                                                     # no job_cursors row at all
        self.assertEqual(r.deferred[0]["missing"], ["fills_ingest", "funding_scan"])
        e2 = self.env()
        e2.repo.coverage_default = DataCoverage(fills_ms=2**62, funding_ms=2**62)
        e2.repo.subs["sub1"] = replace(e2.repo.subs["sub1"], trading_address=None)
        r2 = e2.run(NOW)
        self.assertEqual(r2.profit_share_deferred, 1)                      # fail closed

    def test_margin_after_cutoff(self):
        e = self.env()
        e.cov(self.CUT, self.CUT)                                         # exactly at the cut-off: not enough
        self.assertEqual(e.run(NOW).profit_share_deferred, 1)
        e.cov(self.CUT + timedelta(minutes=2), self.CUT + timedelta(minutes=2))
        self.assertEqual(e.run(NOW + timedelta(minutes=1)).profit_share_settled, 1)

    def test_cancelled_needs_coverage_only_until_cancellation(self):
        e = self.env()
        cancelled = datetime(2026, 10, 1, 15, tzinfo=UTC)
        e.repo.subs["sub1"] = replace(e.repo.subs["sub1"], status="cancelled", cancelled_at=cancelled)
        e.cov(cancelled + timedelta(minutes=10), cancelled + timedelta(minutes=10))  # stopped being tracked later
        r = e.run(NOW)
        self.assertEqual((r.profit_share_deferred, r.profit_share_settled), (0, 1))

    def test_created_after_cutoff_is_not_deferred(self):
        e = self.env()
        e.repo.subs["sub1"] = replace(e.repo.subs["sub1"], created_at=self.CUT + timedelta(minutes=20))
        r = e.run(NOW)
        self.assertEqual(r.profit_share_deferred, 0)

    def test_without_event_sink_uses_alert_sink_with_date_dedup(self):
        e = self.env(events=False)
        e.cov(None, None)
        e.run(NOW)
        a = [x for x in e.alerts.items if x.kind == "settlement_deferred"]
        self.assertEqual(len(a), 1)
        self.assertEqual((a[0].severity, a[0].dedup_key), ("warn", f"settlement_deferred:{D1.isoformat()}"))

    def test_coverage_read_failure_fails_closed(self):
        e = self.env()

        def boom(_addrs):
            raise RuntimeError("db down")
        e.repo.data_coverage = boom
        r = e.run(NOW)
        self.assertEqual(r.profit_share_deferred, 1)
        self.assertIn("data_coverage:RuntimeError", r.errors)

    def test_guard_can_be_disabled(self):
        e = self.env()
        e.s.require_data_coverage = False
        self.assertEqual(e.run(NOW).profit_share_settled, 1)


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
