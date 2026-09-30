"""Final clean-up round against a REAL PostgreSQL 16 (throwaway database, every migration applied — harness from
tests/test_integration_exec_db.py), statements AS the real roles (app_api / app_executor). Each gap is reproduced
first where the old behaviour can be shown, then shown fixed:

1. deposits-scan serves deposit_scan_requests (POST /deposits/usdc/confirm): the requested wallet's own ledger window
   is read first, so a transfer outside the treasury window is credited on the next run; the request is marked
   served; the shared Hyperliquid budget is charged, and when it has no room the request stays pending.
2. /v1/hl/exchange-relay budget: the relay charge lands in hl_rate_budget (app_api may write it) and is refused when
   the minute is spent.
4. admin-paused strategy: no renewal while paused (status untouched), no entries (executor gate; exits allowed),
   paused time credited back on unpause, renewal at the pinned price afterwards.
5. card-hold linkage: a pending profit-share release done by the daily sweep after a CARD top-up paid the debt is
   keyed to that card posting, so payable_card_held() holds it for the dispute window; a USDC-funded one is not held.
6. creator earnings: released pending profit share is `profit_share` (split pro-rata over the strategies that fed it),
   never `other`.

Needs psql and a cluster where the admin URL's user is a superuser (AIJALON_TEST_PG_ADMIN_URL, default
postgresql://postgres@localhost:55432/postgres); skipped otherwise.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
import uuid
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import FakeAlerts  # noqa: E402
from test_integration_exec_db import RUN, LiveDb, RoleRunner, addr  # noqa: E402

from app.config import HlLimits  # noqa: E402
from app.hl.budget import POOL_JOBS, Charge, HlBudgetExhausted  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
T0_MS = int(T0.timestamp() * 1000)
HOUR = 3_600_000


def usd(x: int | str) -> int:
    return int(Decimal(str(x)) * 1_000_000)


class _Db:
    """DatabasePort stand-in: begin() yields the role runner (each statement autocommits)."""

    def __init__(self, runner: RoleRunner) -> None:
        self.runner = runner

    def begin(self):
        return nullcontext(self.runner)


class _Info:
    def __init__(self) -> None:
        self.ledger: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []

    def user_non_funding_ledger_updates(self, user: str, start: int, end: int | None = None) -> list[dict]:
        self.calls.append((user, start, end))
        return [u for u in self.ledger.get(user, []) if start <= u["time"] <= (end or 1 << 62)]


class _Refuse:
    """A shared budget with no room at all (the pacer must stop, never call Hyperliquid)."""

    def try_acquire(self, weight, pool=POOL_JOBS, *, force=False):
        return Charge(granted=False)

    def acquire_wait(self, weight, pool, *, deadline, monotonic, sleep):
        return False


class _Count:
    def __init__(self) -> None:
        self.charged = 0

    def try_acquire(self, weight, pool=POOL_JOBS, *, force=False):
        self.charged += int(weight)
        return Charge(granted=True)

    def acquire_wait(self, weight, pool, *, deadline, monotonic, sleep):
        self.charged += int(weight)
        return True


@unittest.skipUnless(RUN, "needs psql and a reachable PostgreSQL (AIJALON_TEST_PG_ADMIN_URL)")
class CleanupDbTest(unittest.TestCase):
    db_: LiveDb

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_ = LiveDb()
        cls.db_.create()
        if "0014_cleanup.sql" not in cls.db_.applied:
            cls.db_.drop()
            raise AssertionError(f"0014_cleanup.sql did not apply: {cls.db_.note}")
        cls.admin = RoleRunner(cls.db_.url, role=None)
        cls.exe = RoleRunner(cls.db_.url, role="app_executor")
        cls.api = RoleRunner(cls.db_.url, role="app_api")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db_.drop()

    # ------------------------------------------------------------------------------------------------ fixtures
    def user(self, tag: str) -> str:
        t = f"{tag}{uuid.uuid4().hex[:8]}"
        return self.admin.fetchall("""INSERT INTO users (firebase_uid, email, referral_code) VALUES (:u, :e, :c)
                                      RETURNING id::text AS id""", {"u": "fb" + t, "e": f"{t}@x.test", "c": "R" + t})[0]["id"]

    def strategy(self, creator: str, markets: list[str], bps: int = 1000, price: int = 0,
                 status: str = "listed") -> tuple[str, str]:
        sid = self.admin.fetchall("""
            INSERT INTO strategies (slug, name, in_house, owner_user_id, markets, status, price_monthly_micro,
                                    profit_share_bps)
            VALUES (:s, 'T', false, CAST(:o AS uuid), :m, CAST(:st AS strategy_status), :p, :b) RETURNING id::text AS id""",
            {"s": f"cl-{uuid.uuid4().hex[:10]}", "o": creator, "m": markets, "st": status, "p": price, "b": bps})[0]["id"]
        vid = self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, markets)
                                     VALUES (CAST(:s AS uuid), 1, 'h', :m) RETURNING id::text AS id""",
                                  {"s": sid, "m": markets})[0]["id"]
        return sid, vid

    def subscribe(self, uid: str, sid: str, vid: str, address: str, created: datetime,
                  period_end: datetime | None = None, status: str = "active") -> str:
        return self.admin.fetchall("""
            INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address, master_address,
                                       allocation_micro, max_leverage_x100, status, created_at, current_period_end)
            VALUES (CAST(:u AS uuid), CAST(:s AS uuid), CAST(:v AS uuid), :a, :a, 1000000000, 100,
                    CAST(:st AS subscription_status), :c, :pe) RETURNING id::text AS id""",
            {"u": uid, "s": sid, "v": vid, "a": address, "st": status, "c": created,
             "pe": period_end or created + timedelta(days=300)})[0]["id"]

    def deposit(self, uid: str, amount: int) -> None:
        from app.ledger.service import post_transaction

        post_transaction(self.api, f"dep:{uuid.uuid4().hex}", "deposit", "top-up",
                         [("treasury:hl_usdc", amount), (f"user:{uid}:fee_balance", -amount)], "test")

    def bal(self, code: str) -> int:
        from app.ledger.service import get_balance

        return get_balance(self.admin, code)

    def fill(self, sub: str, address: str, t: datetime, tid: int, book_pnl: int, coin: str = "BTC") -> None:
        self.admin.fetchall("""
            INSERT INTO fills (subscription_id, trading_address, coin, tid, oid, px, sz, side, closed_pnl_micro,
                               fee_micro, builder_fee_micro, net_pnl_micro, time, book_pnl_micro, attributed_via)
            VALUES (CAST(:s AS uuid), :a, :coin, :tid, 1, 100, 1, 'sell', 0, 0, 0, 0, :t, :bp, 'cloid')""",
            {"s": sub, "a": address, "coin": coin, "tid": tid, "t": t, "bp": book_pnl})

    def settlement(self, now: datetime) -> Any:
        from app.execution.pg import (PgDatabase, PgLedger, PgPendingReleaser, PgReferralLookup, PgSettlementRepo,
                                      PgUnitOfWork)
        from app.execution.settlement import Settlement
        from app.execution.wiring import DomainBilling, DomainFees, DomainProfitShare

        class Clock:
            def now(self_inner): return now
            def monotonic(self_inner): return 0.0

        db = PgDatabase(self.exe)
        return Settlement(repo=PgSettlementRepo(db), ledger=PgLedger(db), uow=PgUnitOfWork(db),
                          profit_share=DomainProfitShare(), fees=DomainFees(), billing=DomainBilling(72),
                          referrals=PgReferralLookup(db), alerts=FakeAlerts(), clock=Clock(),
                          require_data_coverage=False, pending=PgPendingReleaser(db))

    def psql(self, script: str) -> str:
        r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.db_.url, "-f", "-"],
                           input="\\set VERBOSITY verbose\n" + script, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def post_without_trigger(self, key: str, entries: list[tuple[str, int]]) -> None:
        """A fee-balance credit whose same-transaction pending release did not happen (simulated trigger failure):
        the daily settlement sweep has to release it."""
        js = json.dumps([{"account": a, "amount_micro": m} for a, m in entries])
        self.psql(f"""
            ALTER TABLE ledger_entries DISABLE TRIGGER ledger_entries_20_release_pending;
            SELECT created FROM ledger_post('{key}', 'deposit', 'x', 't', '{js}');
            ALTER TABLE ledger_entries ENABLE TRIGGER ledger_entries_20_release_pending;""")

    def ledger_ok(self) -> None:
        self.assertEqual(self.admin.fetchall("SELECT chain, seq, reason FROM verify_chain()"), [])

    # ============================================================================================== 1. deposits
    def _deposit_world(self):
        treasury = addr("treasury-" + uuid.uuid4().hex)
        uid = self.user("dep")
        w = addr("w-" + uid)
        self.admin.fetchall("INSERT INTO wallets (user_id, master_address, verified_at) VALUES (CAST(:u AS uuid), :a, :t)",
                            {"u": uid, "a": w, "t": T0 - timedelta(days=1)})
        settings = SimpleNamespace(treasury_address=treasury, hl_api_url="https://x",
                                   economics=SimpleNamespace(min_topup_micro=usd(10)))
        return treasury, uid, w, settings

    def _send(self, sender: str, treasury: str, amount: str, t: int) -> dict:
        h = "0x" + hashlib.sha256(f"{sender}{t}{amount}".encode()).hexdigest()
        return {"time": t, "hash": h, "delta": {"type": "send", "user": sender, "destination": treasury,
                                                "sourceDex": "", "destinationDex": "", "token": "USDC",
                                                "amount": amount, "usdcValue": amount, "fee": "0.0",
                                                "nativeTokenFee": "0.0", "nonce": t, "feeToken": ""}}

    def _request(self, uid: str, since: datetime, now: datetime) -> None:
        from app.api.store import SqlStore

        SqlStore().request_deposit_scan(self.api, uid, since=since, now=now)

    def _request_row(self, uid: str) -> dict:
        return self.admin.fetchall("SELECT served_at, requested_at FROM deposit_scan_requests "
                                   "WHERE user_id = CAST(:u AS uuid)", {"u": uid})[0]

    def test_1_scan_request_is_served_first_and_credits_outside_the_treasury_window(self) -> None:
        from app.jobs_data import _db as jdb
        from app.jobs_data.deposits import deposits_scan

        treasury, uid, w, settings = self._deposit_world()
        # a transfer 3 h ago that Hyperliquid showed late: the treasury cursor (now, minus the 60 min overlap) is
        # already past it, so the treasury window never sees it again
        tr = self._send(w, treasury, "25", T0_MS - 3 * HOUR)
        info = _Info()
        info.ledger[treasury] = [tr]
        info.ledger[w] = [tr]
        jdb.set_cursor(self.admin, "deposits", treasury, T0_MS, {"complete": True})
        rep = deposits_scan(_Db(self.exe), T0, info=info, settings=settings, weight_per_minute=100_000)
        self.assertEqual((rep["credited"], rep["errors"]), (0, []))                    # reproduced: never credited
        # the user confirms (API: request row, no HL call) → the next run reads the wallet's own window first
        self._request(uid, T0 - timedelta(hours=4), T0 + timedelta(minutes=1))
        budget = _Count()
        rep = deposits_scan(_Db(self.exe), T0 + timedelta(minutes=5), info=info, settings=settings,
                            weight_per_minute=100_000, rate_budget=budget)
        self.assertEqual((rep["credited"], rep["credited_micro"], rep["scan_requests_served"], rep["errors"]),
                         (1, usd(25), 1, []), rep)
        self.assertEqual(info.calls[-2][0], w)                                           # requested wallet FIRST
        self.assertEqual(info.calls[-1][0], treasury)
        self.assertGreater(budget.charged, 0)                                            # shared budget charged
        self.assertEqual(-self.bal(f"user:{uid}:fee_balance"), usd(25))
        self.assertIsNotNone(self._request_row(uid)["served_at"])
        cur = self.admin.fetchall("SELECT cursor_ms FROM job_cursors WHERE job = 'deposits' AND key = :k",
                                  {"k": treasury})[0]["cursor_ms"]
        self.assertGreaterEqual(cur, T0_MS)                                             # treasury cursor intact
        # served requests are not re-read; the transfer is never credited twice
        n = len(info.calls)
        rep = deposits_scan(_Db(self.exe), T0 + timedelta(minutes=10), info=info, settings=settings,
                            weight_per_minute=100_000)
        self.assertEqual((rep["credited"], rep["scan_requests_served"]), (0, 0))
        self.assertEqual([c[0] for c in info.calls[n:]], [treasury])
        self.ledger_ok()

    def test_1_no_budget_leaves_the_request_pending(self) -> None:
        from app.jobs_data.deposits import deposits_scan

        treasury, uid, w, settings = self._deposit_world()
        info = _Info()
        info.ledger[w] = [self._send(w, treasury, "30", T0_MS - HOUR)]
        self._request(uid, T0 - timedelta(hours=2), T0)
        rep = deposits_scan(_Db(self.exe), T0 + timedelta(minutes=1), info=info, settings=settings,
                            weight_per_minute=100_000, rate_budget=_Refuse(), max_seconds=5.0)
        self.assertEqual(info.calls, [])                                                 # nothing sent to HL
        self.assertEqual((rep["credited"], rep["scan_requests_deferred"]), (0, 1))
        self.assertIsNone(self._request_row(uid)["served_at"])
        rep = deposits_scan(_Db(self.exe), T0 + timedelta(minutes=6), info=info, settings=settings,
                            weight_per_minute=100_000)
        self.assertEqual((rep["credited"], rep["scan_requests_served"]), (1, 1))
        self.assertIsNotNone(self._request_row(uid)["served_at"])

    def test_1_rerequest_during_a_run_stays_pending(self) -> None:
        from app.jobs_data.deposits import deposits_scan

        treasury, uid, w, settings = self._deposit_world()
        self._request(uid, T0 - timedelta(hours=2), T0)
        test = self

        class Info(_Info):
            def user_non_funding_ledger_updates(self, user, start, end=None):
                if user == w:          # the user confirms again while the scan is running
                    test._request(uid, T0 - timedelta(hours=1), T0 + timedelta(minutes=2))
                return super().user_non_funding_ledger_updates(user, start, end)

        rep = deposits_scan(_Db(self.exe), T0 + timedelta(minutes=1), info=Info(), settings=settings,
                            weight_per_minute=100_000)
        self.assertEqual((rep["errors"], rep["scan_requests_served"], rep["scan_requests_deferred"]), ([], 0, 1))
        self.assertIsNone(self._request_row(uid)["served_at"])                           # served next run

    # ============================================================================================== 2. relay budget
    def test_2_relay_charge_lands_in_the_shared_budget_as_app_api(self) -> None:
        from app.hl.budget import HlRateBudget
        from app.hl.relay import charge_relay_budget

        key = "api" + uuid.uuid4().hex[:6]
        limits = HlLimits(egress_key=key, budget_weight_per_minute=100, tick_reserve_per_minute=0)
        b = HlRateBudget(self.api, limits)
        charge_relay_budget(b, limits, max_wait_seconds=0.5)
        self.assertEqual(b.usage()["spent_jobs"], 1)
        self.assertTrue(b.try_acquire(99, POOL_JOBS).granted)                            # minute now full
        with self.assertRaises(HlBudgetExhausted):
            charge_relay_budget(b, limits, max_wait_seconds=0.5)

    # ============================================================================================== 4. admin pause
    def test_4_admin_pause_stops_renewals_and_entries_and_credits_the_paused_time(self) -> None:
        from app.api.store import SqlStore
        from app.execution.pg import PgDatabase, PgSubscriptionRepo
        from app.execution.settlement import renewal_key

        store = SqlStore()
        u, c = self.user("pau"), self.user("pac")
        sid, vid = self.strategy(c, ["BTC"], bps=0, price=usd(30))
        a = addr("pa-" + u)
        created = datetime(2026, 9, 1, 12, tzinfo=UTC)
        sub = self.subscribe(u, sid, vid, a, created, period_end=datetime(2026, 10, 1, 12, tzinfo=UTC))
        # a second subscriber whose period runs across the pause
        u2 = self.user("pau2")
        sub2 = self.subscribe(u2, sid, vid, addr("pa2-" + u2), datetime(2026, 9, 20, 12, tzinfo=UTC),
                              period_end=datetime(2026, 10, 20, 12, tzinfo=UTC))
        self.deposit(u, usd(100))
        self.admin.fetchall("""INSERT INTO user_contacts (user_id, telegram_chat_id, telegram_linked_at, email,
                                                          email_verified_at)
                               VALUES (CAST(:u AS uuid), :c, now(), :e, now()) RETURNING user_id""",
                            {"u": u, "c": int(uuid.uuid4().int % 10**12) + 10**12, "e": f"{u[:8]}@x.test"})
        now_pause = datetime(2026, 9, 25, tzinfo=UTC)
        subs_repo = PgSubscriptionRepo(PgDatabase(self.exe), clock=lambda: now_pause)
        self.assertTrue(subs_repo.get_subscription(sub).entries_allowed)               # contacts OK, listed
        # admin pause (API role)
        told = store.mark_strategy_paused(self.api, sid, now_pause)
        store.set_strategy_status(self.api, sid, "paused")
        self.assertEqual(sorted(str(r["id"]) for r in told), sorted([sub, sub2]))
        self.assertFalse(subs_repo.get_subscription(sub).entries_allowed)              # exits only
        # the renewal falls due while paused: nothing charged, status untouched
        now = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        rep = self.settlement(now).settle_daily(date(2026, 10, 2), now)
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.renewals_skipped_paused, 1)
        self.assertFalse(self.admin.fetchall("SELECT 1 AS x FROM ledger_transactions WHERE idempotency_key = :k",
                                             {"k": renewal_key(sub, datetime(2026, 10, 1, 12, tzinfo=UTC))}))
        self.assertEqual(-self.bal(f"user:{u}:fee_balance"), usd(100))
        st = self.admin.fetchall("SELECT status::text AS s FROM subscriptions WHERE id = CAST(:s AS uuid)", {"s": sub})
        self.assertEqual(st, [{"s": "active"}])
        # unpause 10 days after the pause: sub2's running period gets the 10 days back; sub (ended during the
        # pause) gets its 6 prepaid days back after the unpause
        now_resume = datetime(2026, 10, 5, tzinfo=UTC)
        paused_at, rows = store.resume_strategy_billing(self.api, sid, now_resume)
        store.set_strategy_status(self.api, sid, "listed")
        self.assertIsNotNone(paused_at)
        ends = {r["id"]: r["current_period_end"] for r in self.admin.fetchall(
            "SELECT id::text AS id, current_period_end FROM subscriptions WHERE strategy_id = CAST(:s AS uuid)",
            {"s": sid})}
        self.assertEqual(datetime.fromisoformat(ends[sub2]), datetime(2026, 10, 30, 12, tzinfo=UTC))
        self.assertEqual(datetime.fromisoformat(ends[sub]), datetime(2026, 10, 11, 12, tzinfo=UTC))
        self.assertIsNone(self.admin.fetchall("SELECT paused_at FROM strategies WHERE id = CAST(:s AS uuid)",
                                              {"s": sid})[0]["paused_at"])
        # billing resumes at the pinned price ($30) even if the strategy's price changed meanwhile
        self.admin.fetchall("UPDATE strategies SET price_monthly_micro = :p WHERE id = CAST(:s AS uuid)",
                            {"p": usd(99), "s": sid})
        later = datetime(2026, 10, 12, 0, 30, tzinfo=UTC)
        rep = self.settlement(later).settle_daily(date(2026, 10, 12), later)
        self.assertEqual(rep.errors, [])
        self.assertEqual(rep.renewals_charged_micro, usd(30))
        self.assertEqual(-self.bal(f"user:{u}:fee_balance"), usd(70))
        self.ledger_ok()

    # ============================================================================================== 5. card hold
    def _pending_world(self, tag: str) -> tuple[str, str, str, str]:
        u, c = self.user(tag + "u"), self.user(tag + "c")
        sid, vid = self.strategy(c, ["BTC"], bps=1000)
        a = addr(tag + "-" + u)
        sub = self.subscribe(u, sid, vid, a, datetime(2026, 9, 20, tzinfo=UTC))
        self.fill(sub, a, datetime(2026, 10, 1, 12, tzinfo=UTC), 1, usd(100))
        now = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        self.settlement(now).settle_daily(date(2026, 10, 2), now)
        self.assertEqual(-self.bal(f"ps_pending:{u}:{c}"), usd(10))                     # nothing collected
        return u, c, sid, sub

    def _sweep(self) -> Any:
        now = datetime(2026, 10, 3, 0, 30, tzinfo=UTC)
        return self.settlement(now).settle_daily(date(2026, 10, 3), now)

    def test_5_sweep_release_after_a_card_top_up_is_card_held(self) -> None:
        from app.api.store import SqlStore

        u, c, _sid, _sub = self._pending_world("ch")
        # the debt is paid by a CARD top-up whose trigger release failed
        self.post_without_trigger(f"stripe:pi_{uuid.uuid4().hex[:12]}",
                                  [("stripe:clearing", usd(50)), (f"user:{u}:fee_balance", -usd(50))])
        # unrelated activity afterwards: the newest global ledger seq is NOT the card posting
        self.deposit(self.user("noise"), usd(10))
        rep = self._sweep()
        self.assertGreaterEqual(rep.pending_released_micro, usd("11.5"))
        pay = f"creator:{c}:payable"
        self.assertEqual(-self.bal(pay), usd(10))
        key = self.admin.fetchall("""SELECT idempotency_key AS k FROM ledger_transactions
                                      WHERE kind = 'ps_pending_release' AND idempotency_key LIKE :p""",
                                  {"p": f"ps_release:{u}:%"})[0]["k"]
        card_seq = self.admin.fetchall("""SELECT seq FROM ledger_transactions WHERE idempotency_key LIKE 'stripe:pi_%'
                                           ORDER BY seq DESC LIMIT 1""")[0]["seq"]
        self.assertEqual(key, f"ps_release:{u}:{card_seq}")
        # 0011: card-funded earnings are held for the dispute window (120 days)
        held = SqlStore().payable_card_held(self.api, pay, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(held, usd(10))
        self.ledger_ok()

    def test_5_sweep_release_after_a_usdc_top_up_is_not_held(self) -> None:
        from app.api.store import SqlStore

        u, c, _sid, _sub = self._pending_world("uh")
        self.post_without_trigger(f"usdc_hl:0x{uuid.uuid4().hex}{uuid.uuid4().hex}",
                                  [("treasury:hl_usdc", usd(50)), (f"user:{u}:fee_balance", -usd(50))])
        self._sweep()
        pay = f"creator:{c}:payable"
        self.assertEqual(-self.bal(pay), usd(10))
        self.assertEqual(SqlStore().payable_card_held(self.api, pay, datetime(2026, 1, 1, tzinfo=UTC)), 0)

    def test_5_trigger_release_on_card_top_up_still_held(self) -> None:
        from app.api.store import SqlStore
        from app.ledger.service import post_transaction

        u, c, _sid, _sub = self._pending_world("th")
        post_transaction(self.api, f"stripe:pi_{uuid.uuid4().hex[:12]}", "deposit", "card",
                         [("stripe:clearing", usd(50)), (f"user:{u}:fee_balance", -usd(50))], "test")
        pay = f"creator:{c}:payable"
        self.assertEqual(-self.bal(pay), usd(10))                                        # released by the trigger
        self.assertEqual(SqlStore().payable_card_held(self.api, pay, datetime(2026, 1, 1, tzinfo=UTC)), usd(10))

    # ============================================================================================== 6. earnings
    def test_6_released_pending_is_profit_share_in_the_breakdown(self) -> None:
        from app.api.creator_earnings import earnings_breakdown
        from app.api.store import SqlStore

        u, c, sid, _sub = self._pending_world("er")
        self.deposit(u, usd(50))                                                         # trigger releases
        rows = SqlStore().creator_earnings_by_strategy(self.api, c)
        self.assertEqual(rows, [{"strategy_id": sid, "cat": "profit_share", "micro": usd(10)}])
        per, general, other = earnings_breakdown(
            [{"strategy_id": sid, "slug": "x", "active_subscribers": 1}], rows)
        self.assertEqual((per[0]["profit_share_micro"], per[0]["earned_micro"], general, other), (usd(10), usd(10), 0, 0))

    def test_6_release_fed_by_two_strategies_is_split_exactly(self) -> None:
        from app.api.store import SqlStore

        u, c = self.user("e2u"), self.user("e2c")
        s1, v1 = self.strategy(c, ["BTC"], bps=1000)
        s2, v2 = self.strategy(c, ["ETH"], bps=1000)
        a1, a2 = addr("e2a-" + u), addr("e2b-" + u)
        sub1 = self.subscribe(u, s1, v1, a1, datetime(2026, 9, 20, tzinfo=UTC))
        sub2 = self.subscribe(u, s2, v2, a2, datetime(2026, 9, 20, tzinfo=UTC))
        self.fill(sub1, a1, datetime(2026, 10, 1, 12, tzinfo=UTC), 1, usd(100))
        self.fill(sub2, a2, datetime(2026, 10, 1, 13, tzinfo=UTC), 2, usd("33.333333"), coin="ETH")
        now = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        self.settlement(now).settle_daily(date(2026, 10, 2), now)
        pending = -self.bal(f"ps_pending:{u}:{c}")
        self.deposit(u, usd(7))                                                          # partial release
        rows = SqlStore().creator_earnings_by_strategy(self.api, c)
        self.assertEqual({r["cat"] for r in rows}, {"profit_share"})
        self.assertEqual({r["strategy_id"] for r in rows}, {s1, s2})
        total = sum(int(r["micro"]) for r in rows)
        self.assertEqual(total, -self.bal(f"creator:{c}:payable"))                       # sums exactly
        self.assertEqual(total, pending - (-self.bal(f"ps_pending:{u}:{c}")))
        by = {r["strategy_id"]: int(r["micro"]) for r in rows}
        self.assertGreater(by[s1], by[s2])                                               # ~ 3:1 pro-rata


if __name__ == "__main__":
    unittest.main()
