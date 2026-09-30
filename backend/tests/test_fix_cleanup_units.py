"""Final clean-up round — unit tests (no database, no FastAPI).

- creator signals: a market on a builder dex that is NOT on the active trusted allowlist gets no signal row (the
  version's other markets still do) and a critical ops alert is raised (SPEC §12, REVIEW_TRADING_KEYS F1);
- /v1/hl/exchange-relay: the relayed /exchange request is charged to the shared Hyperliquid budget BEFORE it is sent;
  no room → HlBudgetExhausted and nothing is forwarded;
- admin strategy pause: settlement charges no renewal (and changes no billing status) while the strategy is paused,
  and charges the pinned price once it is listed again; billing_ops pause / resume notify every live subscriber
  (mandatory strategy_paused) and credit the paused time back to running prepaid periods.
"""
from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import FakeAlerts, FakeClock, FakeLedger, FakeReferrals, FakeSettlementRepo, FakeUow, usd  # noqa: E402

from app.config import HlLimits  # noqa: E402
from app.execution.ports import SettlementSubscription  # noqa: E402
from app.hl.budget import POOL_JOBS, Charge, HlBudgetExhausted  # noqa: E402

UTC = timezone.utc


# ================================================================================================ creator signals
class _SqlStub:
    """Just enough of a SqlRunner for PgDatabase: advisory lock granted, no candles table, the trusted allowlist."""

    def __init__(self, trusted: list[str] | None) -> None:
        self.trusted = trusted

    def fetchall(self, sql: str, params: Any = None) -> list[dict]:
        if "pg_try_advisory" in sql:
            return [{"ok": True}]
        if "to_regclass" in sql:
            return [{"ok": False}]
        if "trusted_dexes" in sql:
            if self.trusted is None:
                raise RuntimeError("allowlist unreadable")
            return [{"dex": d} for d in self.trusted]
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


class CreatorSignalsTrustedDexTest(unittest.TestCase):
    MARKETS = ["BTC", "xyz:GOLD", "evil:PUMP"]

    def _run(self, trusted: list[str] | None) -> tuple[dict, list[dict], FakeAlerts]:
        from app.execution import jobs

        stored: list[dict] = []
        now = datetime(2026, 9, 30, 12, 2, tzinfo=UTC)
        iv = jobs.INTERVAL_MS["1h"]
        bar_close_ms = int(now.timestamp() * 1000) // iv * iv

        class Repo:
            def __init__(self, db: Any) -> None:
                pass

            def creator_versions(self) -> list[dict]:
                return [{"version_id": "v1", "strategy_id": "s1", "timeframe": "1h", "lookback": 5,
                         "markets": list(CreatorSignalsTrustedDexTest.MARKETS), "code_hash": "h" * 64,
                         "code_ciphertext": b"sealed", "max_leverage": 3}]

            def has_signal(self, version_id: str, bar_close: datetime) -> bool:
                return False

            def insert_signals(self, *, strategy_id, version_id, bar_close, weights_bps, raw) -> int:
                stored.append({"weights": dict(weights_bps), "raw": dict(raw)})
                return len(weights_bps)

        alerts = FakeAlerts()
        rt = SimpleNamespace(
            bar_settle_seconds=30, missing_bar_grace_seconds=600, creator_signal_budget_seconds=20.0,
            code_decryptor=SimpleNamespace(open_source=lambda **kw: "SOURCE"),
            sandbox=SimpleNamespace(run=lambda src, bars, now_ms: {
                "weights": {"BTC": 0.5, "xyz:GOLD": 0.25, "evil:PUMP": 1.0}, "code_hash": "h" * 64}),
            alerts=lambda db: alerts)
        bars = [{"t": bar_close_ms - iv, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1"}]
        with mock.patch.object(jobs, "PgCreatorSignalRepo", Repo), \
                mock.patch.object(jobs, "_bars_for", lambda *a, **k: list(bars)):
            rep = jobs.run_creator_signals(db=_SqlStub(trusted), now=now, runtime=rt)
        return rep, stored, alerts

    def test_untrusted_market_gets_no_signal_and_an_ops_alert(self) -> None:
        rep, stored, alerts = self._run(["xyz"])
        self.assertEqual((rep["stored"], rep["untrusted"], rep["errors"]), (2, 1, 0), rep)
        self.assertEqual(stored[0]["weights"], {"BTC": 5000, "xyz:GOLD": 2500})       # evil:PUMP never stored
        self.assertEqual(stored[0]["raw"]["untrusted_not_stored"], ["evil:PUMP"])
        crit = [a for a in alerts.items if a.kind == "creator_signal_untrusted_dex"]
        self.assertEqual(len(crit), 1)
        self.assertEqual(crit[0].severity, "critical")
        self.assertEqual(crit[0].payload["dexes"], ["evil"])
        self.assertIsNone(crit[0].user_id)

    def test_removed_dex_and_unreadable_allowlist_fail_closed(self) -> None:
        rep, stored, _ = self._run([])                      # xyz removed from the allowlist
        self.assertEqual(stored[0]["weights"], {"BTC": 5000})
        self.assertEqual(rep["untrusted"], 2)
        rep, stored, alerts = self._run(None)               # allowlist unreadable → validator perps only
        self.assertEqual(stored[0]["weights"], {"BTC": 5000})
        self.assertFalse([a for a in alerts.items if a.kind == "creator_signal_untrusted_dex"][0]
                         .payload["allowlist_loaded"])

    def test_all_trusted_unchanged(self) -> None:
        rep, stored, alerts = self._run(["xyz", "evil"])
        self.assertEqual((rep["stored"], rep["untrusted"]), (3, 0))
        self.assertEqual(alerts.items, [])


# ================================================================================================ relay budget
class _Budget:
    def __init__(self, grant: bool) -> None:
        self.grant = grant
        self.calls: list[tuple[int, str]] = []

    def try_acquire(self, weight: int, pool: str = POOL_JOBS, *, force: bool = False) -> Charge:
        self.calls.append((weight, pool))
        return Charge(granted=self.grant)

    def acquire_wait(self, weight, pool, *, deadline, monotonic, sleep):
        while True:
            if self.try_acquire(weight, pool).granted:
                return True
            if monotonic() + 1.0 >= deadline:
                return False
            sleep(1.0)


class RelayBudgetTest(unittest.TestCase):
    def test_refused_budget_blocks_the_relay(self) -> None:
        from app.hl.relay import charge_relay_budget

        clock = [0.0]
        b = _Budget(grant=False)
        with self.assertRaises(HlBudgetExhausted):
            charge_relay_budget(b, HlLimits(), max_wait_seconds=2.0, monotonic=lambda: clock[0],
                                sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.assertLessEqual(clock[0], 2.0)                         # user requests never queue for long
        self.assertTrue(all(c == (1, POOL_JOBS) for c in b.calls))  # /exchange weight 1, low-priority pool

    def test_granted_budget_is_charged_once(self) -> None:
        from app.hl.relay import charge_relay_budget

        b = _Budget(grant=True)
        charge_relay_budget(b, HlLimits(exchange_weight=3), max_wait_seconds=2.0)
        self.assertEqual(b.calls, [(3, POOL_JOBS)])
        charge_relay_budget(None, HlLimits(), max_wait_seconds=2.0)  # no DB / shared budget off → no accounting

    def test_adapter_charges_before_forwarding(self) -> None:
        """HlInfoAdapter.relay_exchange (needs FastAPI to import app.api.adapters — skipped without it)."""
        try:
            from app.api import adapters
        except ImportError:
            self.skipTest("fastapi not installed")
        order: list[str] = []
        settings = SimpleNamespace(hl_api_url="https://api.hyperliquid.xyz", hl_limits=HlLimits())
        ad = adapters.HlInfoAdapter(settings, db=object())

        class B:
            def __init__(self, db, limits):
                pass

            def acquire_wait(self, *a, **k):
                order.append("charge")
                return False

        with mock.patch("app.hl.budget.HlRateBudget", B), \
                mock.patch("app.hl.relay.forward_exchange", lambda *a, **k: order.append("forward")):
            with self.assertRaises(HlBudgetExhausted):
                ad.relay_exchange({"action": {}, "nonce": 1, "signature": {}})
        self.assertEqual(order, ["charge"])


# ================================================================================================ admin pause: settlement
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)


def _sub(**kw) -> SettlementSubscription:
    base = dict(id="sub1", user_id="user1", strategy_id="s1", creator_user_id="creator1", in_house=False,
                status="active", profit_share_bps=1000, price_monthly_micro=usd(30), cum_pnl_micro=0, hwm_micro=0,
                pnl_cursor=None, current_period_end=datetime(2026, 10, 1, 12, tzinfo=UTC), past_due_since=None,
                created_at=datetime(2026, 9, 1, 12, tzinfo=UTC), trading_address="0x" + "ab" * 20)
    base.update(kw)
    return SettlementSubscription(**base)


class PausedStrategySettlementTest(unittest.TestCase):
    def _env(self):
        from app.execution.settlement import Settlement
        from app.execution.wiring import DomainBilling, DomainFees, DomainProfitShare

        repo, ledger, clock = FakeSettlementRepo(), FakeLedger(), FakeClock(NOW)
        s = Settlement(repo=repo, ledger=ledger, uow=FakeUow(), profit_share=DomainProfitShare(), fees=DomainFees(),
                       billing=DomainBilling(72), referrals=FakeReferrals(None), alerts=FakeAlerts(), clock=clock)
        return s, repo, ledger, clock

    def test_no_renewal_and_no_status_change_while_paused(self) -> None:
        from app.execution.settlement import renewal_key

        s, repo, ledger, _ = self._env()
        repo.subs["sub1"] = _sub(strategy_status="paused")
        repo.subs["sub2"] = _sub(id="sub2", user_id="user2", strategy_status="paused")   # cannot pay: stays active
        ledger.top_up("user1", usd(100))
        rep = s.settle_daily(date(2026, 10, 2), NOW)
        self.assertEqual(rep.errors, [])
        self.assertEqual((rep.renewals_charged, rep.renewals_skipped_paused, rep.renewals_failed), (0, 2, 0))
        self.assertNotIn(renewal_key("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC)), ledger.txs)
        self.assertEqual(ledger.available("user1"), usd(100))
        self.assertEqual([(x.status, x.past_due_since) for x in repo.subs.values()],
                         [("active", None), ("active", None)])

    def test_profit_share_still_settles_while_paused(self) -> None:
        s, repo, ledger, _ = self._env()
        repo.subs["sub1"] = _sub(strategy_status="paused")
        ledger.top_up("user1", usd(100))
        repo.add_pnl("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC), usd(100))        # an exit realised profit
        rep = s.settle_daily(date(2026, 10, 2), NOW)
        self.assertEqual(rep.profit_share_charged_micro, usd("11.5"))

    def test_billing_resumes_at_the_pinned_price_after_unpause(self) -> None:
        from app.execution.settlement import renewal_key

        s, repo, ledger, clock = self._env()
        repo.subs["sub1"] = _sub(strategy_status="paused")
        ledger.top_up("user1", usd(100))
        s.settle_daily(date(2026, 10, 2), NOW)
        # unpaused (strategy listed again; the period was NOT running at pause start → no credit, due now)
        repo.subs["sub1"] = replace(repo.subs["sub1"], strategy_status="listed")
        later = NOW + timedelta(days=1)
        clock.set(later)
        rep = s.settle_daily(date(2026, 10, 3), later)
        self.assertEqual(rep.renewals_charged_micro, usd(30))                       # pinned price
        self.assertIn(renewal_key("sub1", datetime(2026, 10, 1, 12, tzinfo=UTC)), ledger.txs)
        self.assertGreater(repo.subs["sub1"].current_period_end, later)

    def test_pause_landing_after_the_list_was_loaded_is_not_billed(self) -> None:
        s, repo, ledger, _ = self._env()
        repo.subs["sub1"] = _sub()
        ledger.top_up("user1", usd(100))
        repo.lock_for_billing = lambda sid: {"status": "active", "cancelled_at": None,          # type: ignore
                                             "current_period_end": repo.subs[sid].current_period_end,
                                             "strategy_status": "paused"}
        rep = s.settle_daily(date(2026, 10, 2), NOW)
        self.assertEqual((rep.renewals_charged, rep.renewals_skipped_stale), (0, 1))
        self.assertEqual(ledger.available("user1"), usd(100))


# ================================================================================================ admin pause: billing_ops
class _Notifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def notify(self, conn, *, user_id, severity, kind, payload, dedup_key=None) -> None:
        self.sent.append({"user_id": user_id, "severity": severity, "kind": kind, "payload": dict(payload),
                          "dedup_key": dedup_key})


class PauseBillingOpsTest(unittest.TestCase):
    def _svc(self, now: datetime):
        from app.api.testing_cleanup import FakeCleanupStoreMixin

        class Store(FakeCleanupStoreMixin):
            pass

        store = Store()
        store.w = SimpleNamespace(strategies={"s1": {"id": "s1", "status": "listed"}}, subscriptions={})
        clock = [now]
        svc = SimpleNamespace(store=store, notifier=_Notifier(), now=lambda: clock[0])
        return svc, store, clock

    def _add(self, store, sid: str, status: str, end: datetime | None) -> None:
        store.w.subscriptions[sid] = {"id": sid, "user_id": "u-" + sid, "strategy_id": "s1", "status": status,
                                      "current_period_end": end}

    def test_pause_notifies_every_live_subscriber_and_unpause_credits_the_paused_time(self) -> None:
        from app.api import billing_ops

        t_pause = datetime(2026, 10, 10, tzinfo=UTC)
        svc, store, clock = self._svc(t_pause)
        self._add(store, "a", "active", datetime(2026, 10, 20, tzinfo=UTC))      # period running across the pause
        self._add(store, "b", "past_due", datetime(2026, 10, 1, tzinfo=UTC))     # period ended BEFORE the pause
        self._add(store, "c", "paused_user", datetime(2026, 10, 12, tzinfo=UTC))  # ends during the pause
        self._add(store, "d", "cancelled", datetime(2026, 10, 30, tzinfo=UTC))   # not live
        n = billing_ops.pause_strategy_subscriptions(None, svc, strategy_id="s1", strategy_name="Gold")
        self.assertEqual(n, 3)
        paused = [m for m in svc.notifier.sent if m["kind"] == "strategy_paused"]
        self.assertEqual(sorted(m["user_id"] for m in paused), ["u-a", "u-b", "u-c"])
        self.assertTrue(all(m["severity"] == "warn" and m["payload"]["strategy"] == "Gold" for m in paused))
        from app.alerts.prefs import is_mandatory

        self.assertTrue(is_mandatory("strategy_paused"))                           # cannot be muted
        clock[0] = t_pause + timedelta(days=5)
        billing_ops.resume_strategy_subscriptions(None, svc, strategy_id="s1", strategy_name="Gold")
        subs = store.w.subscriptions
        self.assertEqual(subs["a"]["current_period_end"], datetime(2026, 10, 25, tzinfo=UTC))   # +5 days
        self.assertEqual(subs["b"]["current_period_end"], datetime(2026, 10, 1, tzinfo=UTC))    # nothing to credit
        self.assertEqual(subs["c"]["current_period_end"], datetime(2026, 10, 17, tzinfo=UTC))   # 2 prepaid days left
        self.assertEqual(subs["d"]["current_period_end"], datetime(2026, 10, 30, tzinfo=UTC))
        self.assertIsNone(store.w.strategies["s1"]["paused_at"])
        resumed = [m for m in svc.notifier.sent if m["kind"] == "strategy_resumed"]
        self.assertEqual(len(resumed), 3)

    def test_strategy_resumed_renders(self) -> None:
        from app.alerts.user_templates import render_user_alert

        title, body = render_user_alert("strategy_resumed", "info",
                                        {"strategy": "Gold", "period_end": "2026-10-25T00:00:00+00:00"},
                                        web_origin="https://aijalon.trade")
        self.assertEqual(title, "Strategy resumed")
        self.assertIn("Gold", body)


if __name__ == "__main__":
    unittest.main()
