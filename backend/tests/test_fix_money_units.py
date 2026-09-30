"""REVIEW_MONEY money-core fixes — unit tests (no database). Each exploit from docs/security/REVIEW_MONEY.md is
reproduced against the fixed code and shown blocked:

- C1  profit share credited to creators only for what the user's balance covered; the rest is pending, released
      pro-rata on top-up (Python twin of SQL ps_pending_release), subscription past_due.
- H1  per-subscription position book from OUR fills; manual sell / pause / leave are marked to market.
- H2  prefixed cloids / window fallback / oid mismatch never attribute; cloid HMAC keyed by a server secret.
- M3  late fills are claimed by the next settlement (never lost).
- M5  in-memory ledger mirrors the fixed overdraft allowlist and the pending-account rule.
- M7  reconcile solvency invariant.
- L3  HWM advances on a zero-rounded charge (documented policy).
Runs with ``python -m unittest`` (stdlib only).
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
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

from app.domain.profit_share import PositionBook, release_pro_rata, split_collected  # noqa: E402
from app.execution.ports import LedgerLine, PnlDelta, SettlementSubscription  # noqa: E402
from app.execution.settlement import Settlement, profit_share_key  # noqa: E402
from app.execution.wiring import DomainBilling, DomainFees, DomainProfitShare  # noqa: E402

UTC = timezone.utc
D1 = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
CREATED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
U, C = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def sub(**kw) -> SettlementSubscription:
    base = dict(id="sub1", user_id=U, strategy_id="s1", creator_user_id=C, in_house=False,
                status="active", profit_share_bps=1200, price_monthly_micro=0, cum_pnl_micro=0, hwm_micro=0,
                pnl_cursor=None, current_period_end=datetime(2026, 11, 1, 12, 0, tzinfo=UTC), past_due_since=None,
                created_at=CREATED, trading_address="0x" + "ab" * 20)
    base.update(kw)
    return SettlementSubscription(**base)


class ClaimingRepo(FakeSettlementRepo):
    """FakeSettlementRepo with the M3 claim semantics: rows are consumed once, whatever their time."""

    def __init__(self) -> None:
        super().__init__()
        self.claimed: set[tuple[str, int]] = set()
        self.locked: list[str] = []

    def claim_pnl(self, subscription_id, until, settle_date):
        total = 0
        for i, (t, m) in enumerate(self.pnl.get(subscription_id, [])):
            if t <= until and (subscription_id, i) not in self.claimed:
                self.claimed.add((subscription_id, i))
                total += m
        return PnlDelta(realized_micro=total, funding_micro=0, until=until)

    def lock_user(self, user_id):
        self.locked.append(user_id)


class TwinPending:
    """PendingReleaser over the FakeLedger using the Python twin of SQL ps_pending_release."""

    def __init__(self, ledger: FakeLedger) -> None:
        self.ledger = ledger
        self.n = 0

    def users_with_pending(self):
        return sorted({c.split(":")[1] for c, v in self.ledger.balances.items() if c.startswith("ps_pending:") and v < 0})

    def release(self, user_id):
        pending = {c: -v for c, v in self.ledger.balances.items() if c.startswith(f"ps_pending:{user_id}:") and v < 0}
        debt = max(0, self.ledger.balance(f"user:{user_id}:fee_balance"))
        alloc = release_pro_rata(pending, debt)
        if not alloc:
            return 0
        lines = []
        for code, amt in sorted(alloc.items()):
            target = ("platform:revenue:profit_share" if code.endswith(":platform")
                      else f"creator:{code.split(':')[2]}:payable")
            lines += [LedgerLine(code, amt), LedgerLine(target, -amt)]
        self.n += 1
        self.ledger.post_transaction(idempotency_key=f"ps_release:{user_id}:{self.n}", kind="ps_pending_release",
                                     memo="", lines=lines, created_by="t")
        return sum(alloc.values())


class Env:
    def __init__(self, pending: bool = True) -> None:
        self.repo = ClaimingRepo()
        self.ledger = FakeLedger()
        self.alerts = FakeAlerts()
        self.clock = FakeClock(NOW)
        self.pending = TwinPending(self.ledger) if pending else None
        self.s = Settlement(repo=self.repo, ledger=self.ledger, uow=FakeUow(), profit_share=DomainProfitShare(),
                            fees=DomainFees(), billing=DomainBilling(72), referrals=FakeReferrals(),
                            alerts=self.alerts, clock=self.clock, pending=self.pending)

    def run(self, now: datetime, settle_date: date | None = None):
        self.clock.set(now)
        return self.s.settle_daily(settle_date, now)


# ================================================================================================= C1
class C1UncollectedProfitShare(unittest.TestCase):
    def test_colluding_accounts_cannot_mint_creator_payable(self) -> None:
        """REVIEW_MONEY C1 exploit: B1 deposits $10, realises +$2,000 at 12 % → $270 charge. Before: creator payable
        +$240 from money never collected. Now: only the $10 the balance covered reaches payable/revenue."""
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up(U, usd(10))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(2000))
        r = e.run(NOW)
        self.assertEqual(r.errors, [])
        _tx, kind, lines = e.ledger.txs[profit_share_key("sub1", D1)]
        got = {ln.account_code: ln.amount_micro for ln in lines}
        self.assertEqual(kind, "profit_share")
        self.assertEqual(got[f"user:{U}:fee_balance"], usd(270))                  # full charge = user debt
        creator_paid = -got.get(f"creator:{C}:payable", 0)
        platform_paid = -got.get("platform:revenue:profit_share", 0)
        self.assertEqual(creator_paid + platform_paid, usd(10))                    # only what was collected
        self.assertEqual(creator_paid, usd(240) * 10 // 270)                       # pro-rata floor
        self.assertEqual(-got[f"ps_pending:{U}:{C}"] - got[f"ps_pending:{U}:platform"], usd(260))
        self.assertEqual(e.ledger.available(U), -usd(260))
        self.assertEqual((r.profit_share_collected_micro, r.profit_share_pending_micro), (usd(10), usd(260)))
        self.assertIn("profit_share_uncollected", e.alerts.kinds())
        # uncollected > 0 → the subscription goes past_due (billing on the negative balance)
        self.assertEqual(e.repo.subs["sub1"].status, "past_due")
        self.assertEqual(e.repo.locked, [U])                                       # balance read under the user lock
        # the creator can only ever withdraw what is on creator:{id}:payable
        self.assertEqual(-e.ledger.balance(f"creator:{C}:payable"), creator_paid)

    def test_pending_released_pro_rata_on_top_up_and_never_early(self) -> None:
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(1000))  # charge 135 on a $0 balance
        e.run(NOW)
        self.assertEqual(-e.ledger.balance(f"creator:{C}:payable"), 0)
        self.assertEqual(-e.ledger.balance(f"ps_pending:{U}:{C}"), usd(120))
        self.assertEqual(-e.ledger.balance(f"ps_pending:{U}:platform"), usd(15))
        # sweep without a top-up releases nothing
        self.assertEqual(e.s.release_pending(), 0)
        # partial top-up of $35: debt 135 → 100, pending cut to 100 → $35 released pro-rata (120:15)
        e.ledger.top_up(U, usd(35))
        self.assertEqual(e.s.release_pending(), usd(35))
        self.assertEqual(-e.ledger.balance(f"creator:{C}:payable"), usd(120) * 35 // 135 + 1)   # remainder in code order
        self.assertEqual(-e.ledger.balance(f"creator:{C}:payable") - e.ledger.balance("platform:revenue:profit_share"),
                         usd(35))
        # full recovery releases the rest, and the pending accounts end at exactly 0
        e.ledger.top_up(U, usd(200))
        self.assertEqual(e.s.release_pending(), usd(100))
        self.assertEqual(e.ledger.balance(f"ps_pending:{U}:{C}"), 0)
        self.assertEqual(e.ledger.balance(f"ps_pending:{U}:platform"), 0)
        self.assertEqual(-e.ledger.balance(f"creator:{C}:payable"), usd(120))
        self.assertEqual(sum(e.ledger.balances.values()), 0)

    def test_funded_user_is_fully_collected(self) -> None:
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up(U, usd(500))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(1000))
        e.run(NOW)
        lines = {ln.account_code: ln.amount_micro for ln in e.ledger.txs[profit_share_key("sub1", D1)][2]}
        self.assertEqual(lines, {f"user:{U}:fee_balance": usd(135), f"creator:{C}:payable": -usd(120),
                                 "platform:revenue:profit_share": -usd(15)})
        self.assertEqual(e.repo.subs["sub1"].status, "active")

    def test_split_collected_is_exact(self) -> None:
        for c, p, s in [(100, 15, 40), (0, 15, 3), (7, 1, 5), (240_000_000, 30_000_000, 10_000_000), (3, 1, -9),
                        (5, 1, 5), (1, 0, 0), (9, 9, 100)]:
            sp = split_collected(c, p, s)
            self.assertEqual(sp.collected + sp.pending, c + p)
            self.assertEqual(sp.creator_collected + sp.creator_pending, c)
            self.assertEqual(sp.collected, min(c + p, max(0, s)))
            self.assertTrue(0 <= sp.platform_collected <= p and 0 <= sp.creator_collected <= c)

    def test_release_pro_rata_twin(self) -> None:
        self.assertEqual(release_pro_rata({"ps_pending:u:c": 1000, "ps_pending:u:platform": 150}, 550),
                         {"ps_pending:u:c": 522, "ps_pending:u:platform": 78})    # = SQL ps_pending_release
        self.assertEqual(release_pro_rata({"a": 10}, 10), {})
        self.assertEqual(release_pro_rata({"a": 10, "b": 5}, 0), {"a": 10, "b": 5})


# ================================================================================================= H1
class H1PositionBook(unittest.TestCase):
    def test_manual_sell_is_charged_not_evaded(self) -> None:
        """REVIEW_MONEY H1: 1 BTC bought at 60k, price 70k; user sells by hand (foreign), executor re-buys at 70k,
        strategy exits at 70k. Before: attributed ≈ −fees (HL closedPnl uses the account average). Now: +10k − fees."""
        b = PositionBook()
        s1 = b.own_fill(Decimal(1), Decimal(60000), Decimal(60))
        s2 = s1.book.foreign_fill(Decimal(-1), Decimal(70000))                     # manual sell: MTM + closes
        self.assertEqual((s2.pnl_micro, s2.closed_qty, s2.book.qty), (usd(10_000), Decimal(1), 0))
        s3 = s2.book.own_fill(Decimal(1), Decimal(70000), Decimal(70))            # executor re-enters
        s4 = s3.book.own_fill(Decimal(-1), Decimal(70000), Decimal(70), reduce_only=True)
        total = s1.pnl_micro + s2.pnl_micro + s3.pnl_micro + s4.pnl_micro
        self.assertEqual(total, usd(10_000) - usd(200))
        # profit share at 13.5 % on that is charged: 1,323 (creator 1,176) instead of 0
        charge = DomainProfitShare().settle(cum_pnl_micro=0, hwm_micro=0, pnl_delta_micro=total, creator_bps=1200,
                                            in_house=False)
        self.assertEqual(charge.total_micro, total * 1350 // 10_000)

    def test_manual_add_at_worse_price_cannot_inject_a_loss(self) -> None:
        """Reverse H1 exploit: the user adds 1 BTC by hand at a worse price (raising the ACCOUNT average entry); our
        close only realises against OUR entry, the reduce-only excess closing the user's lot is ignored."""
        b = PositionBook().own_fill(Decimal(1), Decimal(60000)).book
        add = b.foreign_fill(Decimal(1), Decimal(80000))                           # adds exposure: not ours
        self.assertEqual((add.pnl_micro, add.book.qty, add.book.avg_px), (0, Decimal(1), b.avg_px))
        close = add.book.own_fill(Decimal(-2), Decimal(70000), reduce_only=True)   # executor flattens 2 on-chain
        self.assertEqual((close.pnl_micro, close.excess_qty, close.book.qty), (usd(10_000), Decimal(1), 0))

    def test_pause_and_leave_mark_to_market(self) -> None:
        b = PositionBook().own_fill(Decimal("-2"), Decimal(100)).book             # short 2 @ 100
        m = b.mark_to_market(Decimal(90))                                          # pause at mark 90: +20
        self.assertEqual((m.pnl_micro, m.book.qty, m.book.avg_px), (usd(20), Decimal(-2), Decimal(90)))
        later = m.book.own_fill(Decimal(2), Decimal(85))                           # close after unpause: +10 only
        self.assertEqual(later.pnl_micro, usd(10))
        self.assertEqual(m.pnl_micro + later.pnl_micro, usd(30))                   # never double charged
        loss = PositionBook(Decimal(1), Decimal(100)).mark_to_market(Decimal("99.5"))
        self.assertEqual(loss.pnl_micro, -usd("0.5"))

    def test_flip_and_rounding_never_overstate(self) -> None:
        b = PositionBook().own_fill(Decimal("0.3"), Decimal("10.1")).book
        s = b.own_fill(Decimal("-0.5"), Decimal("10.2"))                           # closes 0.3, opens short 0.2
        self.assertEqual((s.closed_qty, s.book.qty, s.book.avg_px), (Decimal("0.3"), Decimal("-0.2"), Decimal("10.2")))
        self.assertEqual(s.pnl_micro, 30_000)
        odd = PositionBook().own_fill(Decimal(1), Decimal(1)).book.own_fill(Decimal(2), Decimal(2)).book
        self.assertEqual(odd.avg_px.quantize(Decimal("1e-9")), Decimal("1.666666667"))
        self.assertEqual(odd.own_fill(Decimal(-3), Decimal(2)).pnl_micro, 999_999)  # floored, never 1_000_000


# ================================================================================================= H2
class H2Attribution(unittest.TestCase):
    ADDR = "0x" + "cd" * 20

    def fill(self, tid: int, cloid: str | None, oid: int, side: str = "A", px: str = "100", sz: str = "1",
             closed: str = "-50", builder: str = "0.1") -> dict:
        f = {"coin": "BTC", "px": px, "sz": sz, "side": side, "time": 1_700_000_000_000 + tid, "startPosition": "1",
             "dir": "x", "closedPnl": closed, "hash": "0x" + "00" * 32, "oid": oid, "crossed": True, "fee": "0.2",
             "tid": tid, "feeToken": "USDC", "builderFee": builder}
        if cloid:
            f["cloid"] = cloid
        return f

    def test_prefixed_cloid_and_window_never_attribute(self) -> None:
        """Loss injection: the user tags a losing manual trade with our prefix. Before: attributed via the window.
        Now: not ours (unmatched, alert only), so neither PnL nor builder fee is attributed."""
        from app.hl.client import CLOID_PREFIX
        from app.hl.fills import SubscriptionWindow, attribute_fills

        forged = "0x" + CLOID_PREFIX + "ee" * 12
        win = SubscriptionWindow("sub1", self.ADDR, frozenset({"BTC"}), 0, None)
        res = attribute_fills([self.fill(1, forged, 999)], trading_address=self.ADDR, cloid_to_subscription={},
                              windows=[win], cloid_to_oid={})
        self.assertEqual(res.attributed, [])
        self.assertEqual([f.tid for f in res.ours_unmatched], [1])
        self.assertEqual(res.window_hints, {1: "sub1"})
        self.assertEqual([f.tid for f in res.not_ours()], [1])                    # → treated as a foreign fill (H1)

    def test_recorded_cloid_with_another_oid_is_rejected(self) -> None:
        from app.hl.client import make_cloid
        from app.hl.fills import attribute_fills

        ours = make_cloid("sub1", 1_700_000_000_000, "BTC|0", secret="s" * 32)
        res = attribute_fills([self.fill(1, ours, 111), self.fill(2, ours, 222)], trading_address=self.ADDR,
                              cloid_to_subscription={ours: "sub1"}, cloid_to_oid={ours: 111})
        self.assertEqual([(a.fill.tid, a.oid_verified) for a in res.attributed], [(1, True)])
        self.assertEqual([f.tid for f in res.oid_mismatch], [2])
        # an order without a recorded oid (crash window) is accepted on its secret cloid, oid then recorded
        res2 = attribute_fills([self.fill(3, ours, 333)], trading_address=self.ADDR,
                               cloid_to_subscription={ours: "sub1"}, cloid_to_oid={ours: None})
        self.assertEqual([(a.fill.tid, a.oid_verified) for a in res2.attributed], [(3, True)])

    def test_cloid_not_computable_from_subscription_id(self) -> None:
        """Before: HMAC(key=subscription_id) — the user could compute our cloids. Now keyed with a server secret."""
        import hashlib
        import hmac

        from app.hl.client import CLOID_PREFIX, make_cloid

        bar, leg = 1_700_000_000_000, "BTC|0"
        old = "0x" + CLOID_PREFIX + hmac.new(b"sub1", f"{bar}|{leg}".encode(), hashlib.sha256).hexdigest()[:24]
        a = make_cloid("sub1", bar, leg, secret="server-secret-A" * 3)
        self.assertNotEqual(a, old)
        self.assertNotEqual(a, make_cloid("sub1", bar, leg, secret="server-secret-B" * 3))
        self.assertEqual(a, make_cloid("sub1", bar, leg, secret="server-secret-A" * 3))   # deterministic retries
        self.assertTrue(a.startswith("0x" + CLOID_PREFIX))                                  # prefix stays

    def test_prod_refuses_without_cloid_secret(self) -> None:
        from app.config import get_settings
        from app.errors import ValidationFailed
        from app.hl import client

        prod = replace(get_settings(), env="prod", cloid_secret="")
        orig = client.__dict__.get("get_settings")
        import app.config as cfg

        real = cfg.get_settings
        cfg.get_settings = lambda: prod   # type: ignore[assignment]
        try:
            with self.assertRaises(ValidationFailed):
                client.make_cloid("sub1", 1_700_000_000_000, "BTC|0")
        finally:
            cfg.get_settings = real       # type: ignore[assignment]
        self.assertIsNone(orig)


# ================================================================================================= M3
class M3LateFills(unittest.TestCase):
    def test_late_loss_fill_is_booked_next_day(self) -> None:
        """REVIEW_MONEY M3: a loss fill of day D ingested after D was settled. Before: outside every later window,
        lost forever (the user was over-charged). Now: claimed by the next settlement."""
        e = Env()
        e.repo.subs["sub1"] = sub()
        e.ledger.top_up(U, usd(1000))
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 10, tzinfo=UTC), usd(1000))
        e.run(NOW)
        self.assertEqual(e.repo.subs["sub1"].cum_pnl_micro, usd(1000))
        # the late fill (time on day D, ingested after settlement) + a normal gain on day D+1
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 23, tzinfo=UTC), -usd(400))
        e.repo.add_pnl("sub1", datetime(2026, 10, 2, 12, tzinfo=UTC), usd(500))
        e.run(NOW + timedelta(days=1))
        s = e.repo.subs["sub1"]
        self.assertEqual(s.cum_pnl_micro, usd(1100))                              # the −400 is counted
        self.assertEqual(s.hwm_micro, usd(1100))
        second = e.ledger.txs[profit_share_key("sub1", D1 + timedelta(days=1))][2]
        self.assertEqual({ln.account_code: ln.amount_micro for ln in second}[f"user:{U}:fee_balance"],
                         usd(100) * 1350 // 10_000)                               # charged on +100, not +500

    def test_claim_is_consumed_once(self) -> None:
        e = Env()
        e.repo.subs["sub1"] = sub(profit_share_bps=0, in_house=True, creator_user_id=None)
        e.repo.add_pnl("sub1", datetime(2026, 10, 1, 10, tzinfo=UTC), usd(10))
        e.run(NOW)
        e.run(NOW + timedelta(days=1))
        self.assertEqual(e.repo.subs["sub1"].cum_pnl_micro, usd(10))


# ================================================================================================= M5 (memory twin)
class M5LedgerRules(unittest.TestCase):
    def test_overdraft_only_fee_balance_by_allowlisted_kind(self) -> None:
        from app.errors import InsufficientBalance, ValidationFailed
        from app.ledger.memory import InMemoryLedgerStore
        from app.ledger.service import ensure_account, post_transaction

        st = InMemoryLedgerStore()
        pay = f"creator:{C}:payable"
        ensure_account(st, "payouts:pending", "liability")
        # a payable created as overdraftable is forced non-negative
        acct = st.insert_account(pay, "liability", C, False)
        self.assertTrue(acct.non_negative)
        # profit_share may overdraw the user fee balance …
        post_transaction(st, "ps:1", "profit_share", "", [(f"user:{U}:fee_balance", 100), (pay, -100)], "t")
        # … but no kind (not even profit_share) may overdraw a payable
        with self.assertRaises(InsufficientBalance):
            post_transaction(st, "ph:1", "payout_hold", "", [(pay, 500), ("payouts:pending", -500)], "t")
        with self.assertRaises(InsufficientBalance):
            post_transaction(st, "ps:2", "profit_share", "", [(pay, 500), ("platform:revenue:profit_share", -500)], "t")
        # pending accounts only move by ps_pending_release
        pend = f"ps_pending:{U}:{C}"
        post_transaction(st, "ps:3", "profit_share", "", [(f"user:{U}:fee_balance", 50), (pend, -50)], "t")
        with self.assertRaises(ValidationFailed):
            post_transaction(st, "x:1", "adjustment", "", [(pend, 50), (pay, -50)], "t")
        post_transaction(st, "rel:1", "ps_pending_release", "", [(pend, 50), (pay, -50)], "t")


# ================================================================================================= M7 solvency
class M7Solvency(unittest.TestCase):
    def test_shortfall_alerts(self) -> None:
        from test_execution_fakes import FakeReconcileRepo

        from app.execution.reconcile import Reconciler

        class Solv:
            def solvency_ledger(self):
                return {"user_fee_balances_positive": usd(900), "creator_payables": usd(300), "ps_pending": usd(50),
                        "builder_receivable": usd(20), "stripe_clearing": usd(10), "user_debt": usd(400)}

        class Const:
            def __init__(self, v): self.v = v
            def treasury_usdc_micro(self): return self.v
            def cumulative_builder_rewards_micro(self): return 0

        class Pos:
            def positions(self, address, coins): return {}

        class Led:
            def balance(self, code): return usd(1000) if code == "treasury:hl_usdc" else 0

        alerts = FakeAlerts()
        rec = Reconciler(repo=FakeReconcileRepo(), positions=Pos(), builder_rewards=Const(0), treasury=Const(usd(1000)),
                         ledger=Led(), alerts=alerts, solvency=Solv())
        rep = rec.run("2026-10-02")
        self.assertEqual(rep.solvency_shortfall_micro, usd(1250) - usd(1030))
        self.assertIn("solvency_shortfall", alerts.kinds())
        self.assertEqual(rep.as_dict()["stripe_clearing_status"], "not_configured")


# ================================================================================================= L3
class L3HwmOnZeroCharge(unittest.TestCase):
    def test_hwm_advances_even_when_charge_rounds_to_zero(self) -> None:
        ch = DomainProfitShare().settle(cum_pnl_micro=0, hwm_micro=0, pnl_delta_micro=7, creator_bps=1200,
                                        in_house=False)
        self.assertEqual((ch.total_micro, ch.new_hwm_micro), (0, 7))              # documented policy (≤ 8 µUSD/day)


if __name__ == "__main__":
    unittest.main()
