"""REVIEW_MONEY money-core fixes against a REAL PostgreSQL 16 (throwaway database, every migration applied —
harness from tests/test_integration_exec_db.py). Each exploit of docs/security/REVIEW_MONEY.md is reproduced and
shown blocked, running as the real roles (app_api / app_executor):

- M5  gated overdraft kinds, fixed overdraft allowlist, forced non-negative payables/pending, direct-insert bypass,
      Stripe reversal only through the SECURITY DEFINER wrapper.
- C1  settlement credits the creator only with what was collected; pending released on top-up by the DB; payouts
      from pending impossible; subscription past_due.
- M3  a late fill is claimed by the next settlement; a cancelled subscription with a late fill is still settled.
- H2  builder fee only for oid-verified fills of recorded orders, capped at our rate; forged prefixed fills stay
      unattributed; oid mismatch rejected.
- H1  fills-ingest position book: manual sell marked to market and charged; pause marked to market at the mark.
- L4/L5/M7  account chain, running balances, verify_chain job + anchors (truncation after anchoring detected),
      solvency ledger.

Needs psql and a cluster where the admin URL's user is a superuser (AIJALON_TEST_PG_ADMIN_URL, default
postgresql://postgres@localhost:55432/postgres); skipped otherwise.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import FakeAlerts  # noqa: E402
from test_integration_exec_db import RUN, LiveDb, RoleRunner, addr  # noqa: E402

from app.db.engine import DbError  # noqa: E402
from app.errors import InsufficientBalance, ValidationFailed  # noqa: E402

UTC = timezone.utc
PREFIX = "0xa17a1000"


def usd(x: int | str) -> int:
    return int(Decimal(str(x)) * 1_000_000)


class _FakeInfo:
    """Just enough /info for fills-ingest (userFillsByTime) and the mark price (perpDexs + metaAndAssetCtxs)."""

    def __init__(self) -> None:
        self.fills: dict[str, list[dict]] = {}
        self.marks: dict[str, str] = {}

    def user_fills_by_time(self, user: str, start: int, end: int | None = None, **_: Any) -> list[dict]:
        return sorted([f for f in self.fills.get(user, []) if start <= f["time"] <= (end or 1 << 62)],
                      key=lambda f: f["time"])[:2000]

    def perp_dexs(self) -> list:
        return [None]

    def meta_and_asset_ctxs(self, dex: str = "") -> tuple[dict, list[dict]]:
        names = sorted(self.marks)
        return ({"universe": [{"name": n, "szDecimals": 3, "maxLeverage": 20} for n in names]},
                [{"markPx": self.marks[n], "oraclePx": self.marks[n], "midPx": self.marks[n], "dayNtlVlm": "1000000",
                  "openInterest": "100", "funding": "0", "premium": "0", "prevDayPx": self.marks[n],
                  "impactPxs": None} for n in names])


def _fill(coin: str, side: str, px: str, sz: str, t: int, tid: int, *, start: str = "0", closed: str = "0",
          fee: str = "0", builder: str | None = None, cloid: str | None = None, oid: int = 1) -> dict:
    f = {"coin": coin, "px": px, "sz": sz, "side": side, "time": t, "startPosition": start, "dir": "x",
         "closedPnl": closed, "hash": "0x" + "ab" * 32, "oid": oid, "crossed": True, "fee": fee, "tid": tid,
         "feeToken": "USDC", "twapId": None}
    if builder is not None:
        f["builderFee"] = builder
    if cloid is not None:
        f["cloid"] = cloid
    return f


class _RunnerDb:
    def __init__(self, runner: RoleRunner) -> None:
        self.runner = runner

    def begin(self):
        from contextlib import nullcontext
        return nullcontext(self.runner)


@unittest.skipUnless(RUN, "needs psql and a reachable PostgreSQL (AIJALON_TEST_PG_ADMIN_URL)")
class MoneyFixesDbTest(unittest.TestCase):
    db_: LiveDb

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_ = LiveDb()
        cls.db_.create()
        if "0010_money_fixes.sql" not in cls.db_.applied:
            cls.db_.drop()
            raise AssertionError(f"0010_money_fixes.sql did not apply: {cls.db_.note}")
        cls.admin = RoleRunner(cls.db_.url, role=None)
        cls.exe = RoleRunner(cls.db_.url, role="app_executor")
        cls.api = RoleRunner(cls.db_.url, role="app_api")
        cls.admin.fetchall("""INSERT INTO ledger_accounts (code, kind, non_negative) VALUES
                              ('payouts:pending', 'liability', true), ('withdrawals:pending', 'liability', true)
                              ON CONFLICT (code) DO NOTHING""")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db_.drop()

    # ------------------------------------------------------------------------------------------------ fixtures
    def user(self, tag: str) -> str:
        t = f"{tag}{uuid.uuid4().hex[:8]}"
        return self.admin.fetchall("""INSERT INTO users (firebase_uid, email, referral_code) VALUES (:u, :e, :c)
                                      RETURNING id::text AS id""", {"u": "fb" + t, "e": f"{t}@x.test", "c": "R" + t})[0]["id"]

    def strategy(self, creator: str, markets: list[str], bps: int = 1200) -> tuple[str, str]:
        sid = self.admin.fetchall("""
            INSERT INTO strategies (slug, name, in_house, owner_user_id, markets, status, price_monthly_micro,
                                    profit_share_bps)
            VALUES (:s, 'T', false, CAST(:o AS uuid), :m, 'listed', 0, :b) RETURNING id::text AS id""",
            {"s": f"mf-{uuid.uuid4().hex[:10]}", "o": creator, "m": markets, "b": bps})[0]["id"]
        vid = self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, markets)
                                     VALUES (CAST(:s AS uuid), 1, 'h', :m) RETURNING id::text AS id""",
                                  {"s": sid, "m": markets})[0]["id"]
        return sid, vid

    def subscribe(self, uid: str, sid: str, vid: str, address: str, created: datetime, status: str = "active") -> str:
        return self.admin.fetchall("""
            INSERT INTO subscriptions (user_id, strategy_id, strategy_version_id, trading_address, master_address,
                                       allocation_micro, max_leverage_x100, status, created_at, current_period_end)
            VALUES (CAST(:u AS uuid), CAST(:s AS uuid), CAST(:v AS uuid), :a, :a, 1000000000, 100,
                    CAST(:st AS subscription_status), :c, :pe) RETURNING id::text AS id""",
            {"u": uid, "s": sid, "v": vid, "a": address, "st": status, "c": created,
             "pe": created + timedelta(days=300)})[0]["id"]

    def deposit(self, runner: RoleRunner, uid: str, amount: int) -> None:
        from app.ledger.service import post_transaction

        post_transaction(runner, f"dep:{uuid.uuid4().hex}", "deposit", "top-up",
                         [("treasury:hl_usdc", amount), (f"user:{uid}:fee_balance", -amount)], "test")

    def bal(self, code: str) -> int:
        from app.ledger.service import get_balance

        return get_balance(self.admin, code)

    def insert_fill(self, sub: str | None, address: str, t: datetime, *, tid: int, book_pnl: int | None,
                    px: str = "100", sz: str = "1", side: str = "sell", builder_fee: int = 0, cloid: str | None = None,
                    oid: int = 1, oid_verified: bool = False) -> None:
        self.admin.fetchall("""
            INSERT INTO fills (subscription_id, trading_address, coin, tid, oid, px, sz, side, closed_pnl_micro,
                               fee_micro, builder_fee_micro, net_pnl_micro, cloid, time, book_pnl_micro, oid_verified,
                               attributed_via)
            VALUES (CAST(:s AS uuid), :a, 'BTC', :tid, :oid, CAST(:px AS numeric), CAST(:sz AS numeric),
                    CAST(:side AS order_side), 0, 0, :bf, 0, :cl, :t, :bp, :ov, :via)""",
            {"s": sub, "a": address, "tid": tid, "oid": oid, "px": px, "sz": sz, "side": side, "bf": builder_fee,
             "cl": cloid, "t": t, "bp": book_pnl, "ov": oid_verified, "via": "cloid" if sub else None})

    def settlement(self, now: datetime) -> Any:
        from app.execution.pg import (PgDatabase, PgLedger, PgPendingReleaser, PgReferralLookup, PgSettlementRepo,
                                      PgUnitOfWork)
        from app.execution.settlement import Settlement
        from app.execution.wiring import DomainBilling, DomainFees, DomainProfitShare

        class Clock:
            def now(self_inner): return now
            def monotonic(self_inner): return 0.0

        db = PgDatabase(self.exe)
        self.alerts = FakeAlerts()
        return Settlement(repo=PgSettlementRepo(db), ledger=PgLedger(db), uow=PgUnitOfWork(db),
                          profit_share=DomainProfitShare(), fees=DomainFees(), billing=DomainBilling(72),
                          referrals=PgReferralLookup(db), alerts=self.alerts, clock=Clock(),
                          require_data_coverage=False, pending=PgPendingReleaser(db))

    def psql_script(self, script: str) -> str:
        """Run a multi-statement script as the superuser in ONE session (tamper scenarios inside BEGIN … ROLLBACK)."""
        r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.db_.url, "-f", "-"],
                           input="\\set VERBOSITY verbose\n" + script, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def assert_sqlstate(self, state: str, fn, *args, **kw) -> None:
        with self.assertRaises(DbError) as cm:
            fn(*args, **kw)
        self.assertEqual(cm.exception.sqlstate, state, str(cm.exception))

    def ledger_ok(self) -> None:
        self.assertEqual(self.admin.fetchall("SELECT chain, seq, reason FROM verify_chain()"), [])
        self.assertEqual(self.admin.fetchall("SELECT coalesce(sum(amount_micro), 0)::bigint AS s FROM ledger_entries"),
                         [{"s": 0}])

    # ============================================================================================== M5
    def test_m5_overdraft_is_decided_by_the_db(self) -> None:
        u, c = self.user("m5u"), self.user("m5c")
        fee, pay = f"user:{u}:fee_balance", f"creator:{c}:payable"
        ps = json.dumps([{"account": fee, "amount_micro": 5_000_000_000}, {"account": pay, "amount_micro": -5_000_000_000}])
        # REVIEW exploit 1: app_api posts profit_share and overdraws user2 to −$5,000 → now refused (AJ403)
        self.admin.fetchall("INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES (:c, 'liability', CAST(:o AS uuid), true)",
                            {"c": fee, "o": u})
        self.assert_sqlstate("AJ403", self.api.fetchall,
                             "SELECT created FROM ledger_post(:k, 'profit_share', 'x', 'api', CAST(:e AS jsonb))",
                             {"k": f"t1b:{u}", "e": ps})
        # REVIEW exploit 2: app_api pre-creates the payable with non_negative=false → forced true
        self.api.fetchall("INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative) VALUES (:c, 'liability', NULL, false)",
                          {"c": pay})
        self.assertEqual(self.admin.fetchall("SELECT non_negative, owner_user_id::text AS o FROM ledger_accounts WHERE code = :c",
                                             {"c": pay}), [{"non_negative": True, "o": c}])
        # … so a payout_hold overdrawing it to −$4,000 fails (AJ402)
        hold = json.dumps([{"account": pay, "amount_micro": 4_000_000_000},
                           {"account": "payouts:pending", "amount_micro": -4_000_000_000}])
        self.assert_sqlstate("AJ402", self.api.fetchall,
                             "SELECT created FROM ledger_post(:k, 'payout_hold', 'x', 'api', CAST(:e AS jsonb))",
                             {"k": f"ph:{c}", "e": hold})
        # a payable with the wrong owner is refused outright
        self.assert_sqlstate("AJ422", self.api.fetchall,
                             "INSERT INTO ledger_accounts (code, kind, owner_user_id) VALUES (:c, 'liability', CAST(:o AS uuid))",
                             {"c": f"referrer:{c}:payable", "o": u})
        # even the owner cannot create a protected account overdraftable (CHECK, also under replica)
        self.assert_sqlstate("23514", self.admin.fetchall,
                             "BEGIN; ALTER TABLE ledger_accounts DISABLE TRIGGER ledger_accounts_10_shape; "
                             "INSERT INTO ledger_accounts (code, kind, non_negative) VALUES ('suspense:mf_test', 'liability', false); "
                             "ROLLBACK")
        # direct INSERT of a profit_share tx by app_api (bypassing ledger_post) → refused at COMMIT (no authorisation)
        self.assert_sqlstate("AJ403", self.api.fetchall, f"""
            BEGIN;
            INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest)
              VALUES ('direct:{u}', 'profit_share', 't', ledger_entries_digest(ARRAY['{fee}', '{pay}'], ARRAY[7, -7]::bigint[]));
            INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
              SELECT t.id, a.id, 7 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'direct:{u}' AND a.code = '{fee}';
            INSERT INTO ledger_entries (tx_id, account_id, amount_micro)
              SELECT t.id, a.id, -7 FROM ledger_transactions t, ledger_accounts a WHERE t.idempotency_key = 'direct:{u}' AND a.code = '{pay}';
            COMMIT""")
        # … nor can it forge the authorisation row
        self.assert_sqlstate("42501", self.api.fetchall,
                             "INSERT INTO ledger_tx_authorizations (tx_id, kind, authorized_as) SELECT id, kind, 'x' FROM ledger_transactions LIMIT 1")
        # app_executor may post profit_share (settlement) — but even it cannot credit a creator with uncollected
        # profit share (C1 enforced in the DB: the user's balance before the charge is $0)
        self.assert_sqlstate("AJ402", self.exe.fetchall,
                             "SELECT created FROM ledger_post(:k, 'profit_share', 'x', 'exe', CAST(:e AS jsonb))",
                             {"k": f"ps-c1:{u}", "e": json.dumps([{"account": fee, "amount_micro": 100},
                                                                  {"account": pay, "amount_micro": -100}])})
        self.assert_sqlstate("AJ403", self.exe.fetchall,        # nor credit anything else (e.g. a referrer payable)
                             "SELECT created FROM ledger_post(:k, 'profit_share', 'x', 'exe', CAST(:e AS jsonb))",
                             {"k": f"ps-sh:{u}", "e": json.dumps([{"account": fee, "amount_micro": 100},
                                                                  {"account": "stripe:clearing", "amount_micro": -100}])})
        self.admin.fetchall("INSERT INTO ledger_accounts (code, kind, owner_user_id) VALUES (:c, 'liability', NULL)",
                            {"c": f"ps_pending:{u}:{c}"})
        self.assertEqual(self.exe.fetchall("SELECT created FROM ledger_post(:k, 'profit_share', 'x', 'exe', CAST(:e AS jsonb))",
                                           {"k": f"ps-ok:{u}", "e": json.dumps([{"account": fee, "amount_micro": 100},
                                                                                {"account": f"ps_pending:{u}:{c}",
                                                                                 "amount_micro": -100}])}),
                         [{"created": True}])
        self.assert_sqlstate("42501", self.exe.fetchall,        # the executor cannot forge an authorisation row
                             "INSERT INTO ledger_tx_authorizations (tx_id, kind, authorized_as) SELECT id, kind, 'x' FROM ledger_transactions LIMIT 1")
        self.assert_sqlstate("AJ403", self.exe.fetchall,
                             "SELECT created FROM ledger_post(:k, 'ps_pending_release', 'x', 'exe', CAST(:e AS jsonb))",
                             {"k": f"rel:{u}", "e": json.dumps([{"account": pay, "amount_micro": 1},
                                                                {"account": "platform:revenue:posts", "amount_micro": -1}])})
        # Stripe dispute (webhook path, app_api) goes through the SECURITY DEFINER wrapper via PostgresLedgerStore
        from app.ledger.service import post_transaction

        tx = post_transaction(self.api, f"stripe:dispute:dp_{uuid.uuid4().hex[:8]}", "stripe_dispute", "dispute",
                              [(fee, 250), ("stripe:clearing", -250)], "system:stripe")
        self.assertTrue(tx.created)
        # … whose fixed shape refuses moving the overdraft anywhere but stripe:clearing (e.g. into a revenue account)
        with self.assertRaises(ValidationFailed):
            post_transaction(self.api, f"stripe:dispute:dp_{uuid.uuid4().hex[:8]}", "stripe_dispute", "dispute",
                             [(fee, 250), ("platform:revenue:posts", -250)], "system:stripe")
        with self.assertRaises((ValidationFailed, InsufficientBalance)):
            post_transaction(self.api, f"stripe:dispute:dp_{uuid.uuid4().hex[:8]}", "stripe_dispute", "dispute",
                             [(pay, 250), ("stripe:clearing", -250)], "system:stripe")
        with self.assertRaises(ValidationFailed):      # key does not match the kind
            post_transaction(self.api, f"deposit:{uuid.uuid4().hex[:8]}", "stripe_refund", "x",
                             [(fee, 1), ("stripe:clearing", -1)], "system:stripe")
        # a non-allowlisted kind can never overdraw even the fee balance
        with self.assertRaises(InsufficientBalance):
            post_transaction(self.api, f"post:{uuid.uuid4().hex[:8]}", "post_purchase", "x",
                             [(fee, 1), ("platform:revenue:posts", -1)], "api")
        self.ledger_ok()

    # ============================================================================================== C1
    def test_c1_creator_paid_only_what_was_collected(self) -> None:
        from app.execution.settlement import profit_share_key

        u, c = self.user("c1u"), self.user("c1c")
        sid, vid = self.strategy(c, ["BTC"], bps=1200)
        a = addr("c1-" + u)
        day = date(2026, 10, 1)
        sub = self.subscribe(u, sid, vid, a, datetime(2026, 9, 20, tzinfo=UTC))
        self.deposit(self.api, u, usd(10))
        # B1 realised +$2,000 on day D (our book)
        self.insert_fill(sub, a, datetime(2026, 10, 1, 12, tzinfo=UTC), tid=1, book_pnl=usd(2000))
        now = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        rep = self.settlement(now).settle_daily(date(2026, 10, 2), now)
        self.assertEqual(rep.errors, [], rep)
        fee, pay = f"user:{u}:fee_balance", f"creator:{c}:payable"
        self.assertEqual(-self.bal(fee), -usd(260))                               # user owes the uncollected part
        creator_paid = -self.bal(pay)
        self.assertEqual(creator_paid, usd(240) * 10 // 270)                      # NOT $240
        self.assertEqual(creator_paid - self.bal("platform:revenue:profit_share") >= usd(10), True)
        pend_c, pend_p = -self.bal(f"ps_pending:{u}:{c}"), -self.bal(f"ps_pending:{u}:platform")
        self.assertEqual(pend_c + pend_p, usd(260))
        self.assertEqual(self.admin.fetchall("SELECT status::text AS s FROM subscriptions WHERE id = CAST(:s AS uuid)",
                                             {"s": sub}), [{"s": "past_due"}])
        # the creator tries to withdraw the $240 → AJ402; from the pending account → AJ403 (never payable)
        from app.ledger.service import post_transaction

        with self.assertRaises(InsufficientBalance):
            post_transaction(self.api, f"payout:{uuid.uuid4().hex}:hold", "payout_hold", "x",
                             [(pay, usd(240)), ("payouts:pending", -usd(240))], "api")
        with self.assertRaises(ValidationFailed):
            post_transaction(self.api, f"payout:{uuid.uuid4().hex}:hold", "payout_hold", "x",
                             [(f"ps_pending:{u}:{c}", 1), ("payouts:pending", -1)], "api")
        # a partial top-up of $100 (API role, deposit path) releases $100 of pending pro-rata, in the same tx
        self.deposit(self.api, u, usd(100))
        self.assertEqual(-self.bal(fee), -usd(160))
        self.assertEqual(-self.bal(f"ps_pending:{u}:{c}") - self.bal(f"ps_pending:{u}:platform"), usd(160))
        self.assertEqual(-self.bal(pay), creator_paid + (usd(100) * pend_c) // (pend_c + pend_p)
                         + (1 if (usd(100) * pend_c) % (pend_c + pend_p) else 0))
        rel = self.admin.fetchall("SELECT kind, created_by FROM ledger_transactions WHERE idempotency_key LIKE :p",
                                  {"p": f"ps_release:{u}:%"})
        self.assertEqual(rel, [{"kind": "ps_pending_release", "created_by": "system:ps_release_on_topup"}])
        # full recovery: everything released, creator ends with exactly its 12 % share
        self.deposit(self.api, u, usd(300))
        self.assertEqual(self.bal(f"ps_pending:{u}:{c}"), 0)
        self.assertEqual(self.bal(f"ps_pending:{u}:platform"), 0)
        self.assertEqual(-self.bal(pay), usd(240))
        # settlement sweep is a no-op now; re-settling the same day changes nothing
        n = self.admin.fetchall("SELECT count(*) AS n FROM ledger_transactions")[0]["n"]
        rep2 = self.settlement(now + timedelta(minutes=5)).settle_daily(date(2026, 10, 2), now + timedelta(minutes=5))
        self.assertEqual((rep2.profit_share_charged_micro, rep2.pending_released_micro), (0, 0))
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM ledger_transactions")[0]["n"], n)
        self.assertTrue(self.admin.fetchall("SELECT 1 AS x FROM ledger_transactions WHERE idempotency_key = :k",
                                            {"k": profit_share_key(sub, date(2026, 10, 2))}))
        # solvency ledger view sees the payables, pending 0, no debt
        from app.execution.pg import PgDatabase, PgReconcileRepo

        sol = PgReconcileRepo(PgDatabase(self.exe)).solvency_ledger()
        self.assertGreaterEqual(sol["creator_payables"], usd(240))
        self.ledger_ok()

    def test_c1_settlement_sweep_releases_when_trigger_did_not(self) -> None:
        u, c = self.user("swu"), self.user("swc")
        sid, vid = self.strategy(c, ["BTC"], bps=1000)
        a = addr("sw-" + u)
        sub = self.subscribe(u, sid, vid, a, datetime(2026, 9, 20, tzinfo=UTC))
        self.insert_fill(sub, a, datetime(2026, 10, 1, 12, tzinfo=UTC), tid=1, book_pnl=usd(100))
        now = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        self.settlement(now).settle_daily(date(2026, 10, 2), now)
        self.assertEqual(-self.bal(f"ps_pending:{u}:{c}"), usd(10))
        # a top-up whose trigger release is suppressed (simulated failure) → the daily sweep releases it
        self.psql_script(f"""
            ALTER TABLE ledger_entries DISABLE TRIGGER ledger_entries_20_release_pending;
            SELECT created FROM ledger_post('sw:{u}', 'deposit', 'x', 't',
              '[{{"account":"treasury:hl_usdc","amount_micro":50000000}},{{"account":"user:{u}:fee_balance","amount_micro":-50000000}}]');
            ALTER TABLE ledger_entries ENABLE TRIGGER ledger_entries_20_release_pending;""")
        self.assertEqual(-self.bal(f"ps_pending:{u}:{c}"), usd(10))
        rep = self.settlement(now + timedelta(days=1)).settle_daily(date(2026, 10, 3), now + timedelta(days=1))
        self.assertGreaterEqual(rep.pending_released_micro, usd("11.5"))
        self.assertEqual(self.bal(f"ps_pending:{u}:{c}"), 0)
        self.assertEqual(-self.bal(f"creator:{c}:payable"), usd(10))
        self.ledger_ok()

    # ============================================================================================== M3
    def test_m3_late_fill_booked_next_settlement(self) -> None:
        u, c = self.user("m3u"), self.user("m3c")
        sid, vid = self.strategy(c, ["BTC"], bps=1000)
        a = addr("m3-" + u)
        sub = self.subscribe(u, sid, vid, a, datetime(2026, 9, 20, tzinfo=UTC))
        self.deposit(self.api, u, usd(1000))
        self.insert_fill(sub, a, datetime(2026, 10, 1, 10, tzinfo=UTC), tid=1, book_pnl=usd(1000))
        d1 = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        self.settlement(d1).settle_daily(date(2026, 10, 2), d1)
        # a LOSS fill of day D arrives after D was settled (before the fix: lost; user over-charged forever)
        self.insert_fill(sub, a, datetime(2026, 10, 1, 23, 59, tzinfo=UTC), tid=2, book_pnl=-usd(400))
        self.insert_fill(sub, a, datetime(2026, 10, 2, 9, tzinfo=UTC), tid=3, book_pnl=usd(500))
        d2 = d1 + timedelta(days=1)
        self.settlement(d2).settle_daily(date(2026, 10, 3), d2)
        s = self.admin.fetchall("SELECT cum_pnl_micro AS c, hwm_micro AS h FROM subscriptions WHERE id = CAST(:s AS uuid)",
                                {"s": sub})[0]
        self.assertEqual((s["c"], s["h"]), (usd(1100), usd(1100)))
        self.assertEqual(self.admin.fetchall("""SELECT tid, ps_settlement_date::text AS d FROM fills
                                                 WHERE subscription_id = CAST(:s AS uuid) ORDER BY tid""", {"s": sub}),
                         [{"tid": 1, "d": "2026-10-02"}, {"tid": 2, "d": "2026-10-03"}, {"tid": 3, "d": "2026-10-03"}])
        # a cancelled subscription with a late fill after its final settlement stays in scope and is settled
        self.admin.fetchall("""UPDATE subscriptions SET status = 'cancelled', cancel_positions = 'leave',
                               cancelled_at = '2026-10-02T12:00:00Z' WHERE id = CAST(:s AS uuid)""", {"s": sub})
        d3 = d2 + timedelta(days=1)
        self.settlement(d3).settle_daily(date(2026, 10, 4), d3)
        self.insert_fill(sub, a, datetime(2026, 10, 2, 11, tzinfo=UTC), tid=4, book_pnl=usd(100))
        d4 = d3 + timedelta(days=1)
        from app.execution.pg import PgDatabase, PgSettlementRepo

        self.assertIn(sub, [x.id for x in PgSettlementRepo(PgDatabase(self.exe)).subscriptions_to_settle()])
        self.settlement(d4).settle_daily(date(2026, 10, 5), d4)
        self.assertEqual(self.admin.fetchall("SELECT cum_pnl_micro AS c FROM subscriptions WHERE id = CAST(:s AS uuid)",
                                             {"s": sub}), [{"c": usd(1200)}])
        self.ledger_ok()

    # ============================================================================================== H2
    def test_h2_builder_fee_only_for_verified_fills_and_capped(self) -> None:
        u, c = self.user("h2u"), self.user("h2c")
        sid, vid = self.strategy(c, ["BTC"])
        a = addr("h2-" + u)
        sub = self.subscribe(u, sid, vid, a, datetime(2026, 9, 20, tzinfo=UTC))
        ours = PREFIX + "11" * 12
        self.admin.fetchall("""INSERT INTO orders (subscription_id, cloid, coin, side, sz, limit_px, status, oid)
                               VALUES (CAST(:s AS uuid), :c, 'BTC', 'sell', 1, 100, 'filled', 77)""", {"s": sub, "c": ours})
        t = datetime(2026, 10, 1, 12, tzinfo=UTC)
        # (a) our verified fill; the reported builder fee (0.5) exceeds 0.1 % of $100 notional → capped at $0.10
        self.insert_fill(sub, a, t, tid=1, book_pnl=0, builder_fee=usd("0.5"), cloid=ours, oid=77, oid_verified=True)
        # (b) user's prefixed-cloid order with their own builder: stored unattributed → nothing recognised
        self.insert_fill(None, a, t, tid=2, book_pnl=None, builder_fee=usd(5), cloid=PREFIX + "22" * 12, oid=78)
        # (c) attributed but the oid was never verified (e.g. forged cloid reuse) → nothing recognised
        self.insert_fill(sub, a, t, tid=3, book_pnl=0, builder_fee=usd(5), cloid=ours, oid=79, oid_verified=False)
        now = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        rep = self.settlement(now).settle_daily(date(2026, 10, 2), now)
        self.assertEqual(rep.errors, [])
        got = self.admin.fetchall("""SELECT t.idempotency_key AS k, e.amount_micro AS amt FROM ledger_transactions t
                                      JOIN ledger_entries e ON e.tx_id = t.id JOIN ledger_accounts a ON a.id = e.account_id
                                     WHERE t.idempotency_key LIKE :p AND a.code = 'builder:hl_receivable'""",
                                  {"p": f"bf:{a}:%"})
        self.assertEqual(got, [{"k": f"bf:{a}:1", "amt": usd("0.1")}])       # capped, only the verified fill
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM fills WHERE trading_address = :a AND builder_fee_recognised_at IS NULL",
                                             {"a": a}), [{"n": 0}])
        self.ledger_ok()

    # ============================================================================================== H1 (+H2 ingest)
    def test_h1_manual_sell_marked_to_market_and_charged(self) -> None:
        from app.jobs_data.fills import fills_ingest

        u, c = self.user("h1u"), self.user("h1c")
        sid, vid = self.strategy(c, ["BTC"], bps=1200)
        a = addr("h1-" + u)
        created = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
        sub = self.subscribe(u, sid, vid, a, created)
        self.deposit(self.api, u, usd(5000))
        c_buy, c_rebuy, c_exit = PREFIX + "a1" * 12, PREFIX + "a2" * 12, PREFIX + "a3" * 12
        for cl, side, ro in ((c_buy, "buy", False), (c_rebuy, "buy", False), (c_exit, "sell", True)):
            self.admin.fetchall("""INSERT INTO orders (subscription_id, cloid, coin, side, sz, limit_px, status, reduce_only)
                                   VALUES (CAST(:s AS uuid), :c, 'BTC', CAST(:side AS order_side), 1, 1, 'filled', :ro)""",
                                {"s": sub, "c": cl, "side": side, "ro": ro})
        t0 = int(datetime(2026, 10, 1, 8, tzinfo=UTC).timestamp() * 1000)
        info = _FakeInfo()
        info.fills[a] = [
            _fill("BTC", "B", "60000", "1", t0, 101, start="0", fee="60", builder="60", cloid=c_buy, oid=11),
            # the user sells the strategy's BTC by hand at 70k (foreign; HL closedPnl +10k on the account)
            _fill("BTC", "A", "70000", "1", t0 + 3_600_000, 102, start="1", closed="10000", fee="30"),
            _fill("BTC", "B", "70000", "1", t0 + 7_200_000, 103, start="0", fee="70", builder="70", cloid=c_rebuy, oid=13),
            _fill("BTC", "A", "70000", "1", t0 + 10_800_000, 104, start="1", closed="0", fee="70", builder="70",
                  cloid=c_exit, oid=14),
            # a forged order carrying our recorded cloid but another oid (would inject a loss) → rejected
            _fill("BTC", "A", "50000", "0.1", t0 + 10_900_000, 105, start="0", closed="-1000", fee="5", cloid=c_buy,
                  oid=999),
        ]
        now = datetime(2026, 10, 1, 23, 50, tzinfo=UTC)
        rep = fills_ingest(_RunnerDb(self.exe), now, info=info, weight_per_minute=100_000)
        self.assertEqual(rep["errors"], [], rep)
        # the forged fill is not ours: not stored, and recorded as a foreign fill (book already flat → no PnL)
        self.assertEqual((rep["attributed"], rep["oid_mismatch"], rep["foreign_book_events"]), (3, 1, 2), rep)
        rows = {r["tid"]: r for r in self.admin.fetchall(
            """SELECT tid, book_pnl_micro AS b, net_pnl_micro AS n, oid_verified AS ov FROM fills
                WHERE trading_address = :a ORDER BY tid""", {"a": a})}
        self.assertEqual(sorted(rows), [101, 103, 104])                           # foreign / forged not stored
        self.assertEqual([rows[t]["b"] for t in (101, 103, 104)], [-usd(60), -usd(70), -usd(70)])
        self.assertTrue(all(r["ov"] for r in rows.values()))
        ev = self.admin.fetchall("""SELECT kind, ref, pnl_micro AS p, qty_after::text AS qty FROM subscription_pnl_events
                                     WHERE subscription_id = CAST(:s AS uuid)""", {"s": sub})
        self.assertEqual(sorted((e["kind"], e["ref"], e["p"]) for e in ev),
                         [("foreign_fill", "102", usd(10_000)), ("foreign_fill", "105", 0)])
        self.assertEqual(len(self.admin.fetchall("SELECT 1 AS x FROM events_outbox WHERE dedup_key = :d",
                                                 {"d": f"foreign_trade:{sub}:102"})), 1)
        self.assertEqual(len(self.admin.fetchall("SELECT 1 AS x FROM events_outbox WHERE dedup_key = :d",
                                                 {"d": f"ops:fill_oid_mismatch:{a}:105"})) +
                         len(self.admin.fetchall("SELECT 1 AS x FROM events_outbox WHERE dedup_key = :d",
                                                 {"d": f"fill_oid_mismatch:{a}:105"})), 1)
        # re-run (overlap re-fetch): idempotent
        rep2 = fills_ingest(_RunnerDb(self.exe), now + timedelta(minutes=5), info=info, weight_per_minute=100_000)
        self.assertEqual((rep2["inserted"], rep2["foreign_book_events"]), (0, 0), rep2)
        book = self.admin.fetchall("SELECT qty::text AS qty FROM subscription_positions WHERE subscription_id = CAST(:s AS uuid)",
                                   {"s": sub})
        self.assertEqual(Decimal(book[0]["qty"]), 0)
        # settlement charges 13.5 % on +10,000 − 200 fees (before the fix: on HL closedPnl − fees ≈ −200 → nothing)
        d = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        r = self.settlement(d).settle_daily(date(2026, 10, 2), d)
        self.assertEqual(r.profit_share_charged_micro, (usd(10_000) - usd(200)) * 1350 // 10_000)
        ps = self.admin.fetchall("""SELECT e.amount_micro AS amt FROM ledger_transactions t
                                     JOIN ledger_entries e ON e.tx_id = t.id JOIN ledger_accounts a ON a.id = e.account_id
                                    WHERE t.idempotency_key = :k AND a.code = :c""",
                                 {"k": f"ps:{sub}:2026-10-02", "c": f"creator:{c}:payable"})
        self.assertEqual(ps, [{"amt": -((usd(10_000) - usd(200)) * 1200 // 10_000)}])
        # + 50 % of the $200 builder fees of our three oid-verified fills
        self.assertEqual(-self.bal(f"creator:{c}:payable"), (usd(10_000) - usd(200)) * 1200 // 10_000 + usd(100))
        self.ledger_ok()

    def test_h1_pause_and_leave_marked_to_market(self) -> None:
        from app.jobs_data.fills import fills_ingest

        u, c = self.user("pzu"), self.user("pzc")
        sid, vid = self.strategy(c, ["BTC"], bps=1000)
        a = addr("pz-" + u)
        sub = self.subscribe(u, sid, vid, a, datetime(2026, 10, 1, tzinfo=UTC))
        cl = PREFIX + "b1" * 12
        self.admin.fetchall("""INSERT INTO orders (subscription_id, cloid, coin, side, sz, limit_px, status)
                               VALUES (CAST(:s AS uuid), :c, 'BTC', 'buy', 2, 1, 'filled')""", {"s": sub, "c": cl})
        t0 = int(datetime(2026, 10, 1, 8, tzinfo=UTC).timestamp() * 1000)
        info = _FakeInfo()
        info.fills[a] = [_fill("BTC", "B", "100", "2", t0, 201, fee="0.2", cloid=cl, oid=21)]
        info.marks["BTC"] = "130"
        now = datetime(2026, 10, 1, 12, tzinfo=UTC)
        fills_ingest(_RunnerDb(self.exe), now, info=info, weight_per_minute=100_000)
        # the user pauses with the position open (then could sell by hand and unpause)
        self.admin.fetchall("""UPDATE subscriptions SET status = 'paused_user', status_changed_at = :t
                               WHERE id = CAST(:s AS uuid)""", {"s": sub, "t": now + timedelta(minutes=1)})
        rep = fills_ingest(_RunnerDb(self.exe), now + timedelta(minutes=10), info=info, weight_per_minute=100_000)
        self.assertEqual(rep["mtm_events"], 1, rep)
        ev = self.admin.fetchall("""SELECT kind, pnl_micro AS p, avg_after::text AS avg, qty_after::text AS qty
                                      FROM subscription_pnl_events WHERE subscription_id = CAST(:s AS uuid)""", {"s": sub})
        self.assertEqual([(e["kind"], e["p"]) for e in ev], [("mtm_pause", usd(60))])
        self.assertEqual((Decimal(ev[0]["avg"]), Decimal(ev[0]["qty"])), (Decimal(130), Decimal(2)))
        again = fills_ingest(_RunnerDb(self.exe), now + timedelta(minutes=20), info=info, weight_per_minute=100_000)
        self.assertEqual(again["mtm_events"], 0)                                  # once per status change
        # cancel "leave" at a new mark: the remaining move is attributed, the book is closed
        info.marks["BTC"] = "125"
        self.admin.fetchall("""UPDATE subscriptions SET status = 'cancelled', cancel_positions = 'leave',
                               cancelled_at = :t, status_changed_at = :t WHERE id = CAST(:s AS uuid)""",
                            {"s": sub, "t": now + timedelta(minutes=30)})
        fills_ingest(_RunnerDb(self.exe), now + timedelta(minutes=40), info=info, weight_per_minute=100_000)
        ev = self.admin.fetchall("""SELECT kind, pnl_micro AS p FROM subscription_pnl_events
                                     WHERE subscription_id = CAST(:s AS uuid) ORDER BY created_at""", {"s": sub})
        self.assertEqual([(e["kind"], e["p"]) for e in ev], [("mtm_pause", usd(60)), ("mtm_leave", -usd(10))])
        self.assertEqual(Decimal(self.admin.fetchall(
            "SELECT qty::text AS qty FROM subscription_positions WHERE subscription_id = CAST(:s AS uuid)", {"s": sub})[0]["qty"]), 0)
        # settlement charges on fills + adjustments: −0.2 + 60 − 10
        self.deposit(self.api, u, usd(100))
        d = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
        self.settlement(d).settle_daily(date(2026, 10, 2), d)
        self.assertEqual(self.admin.fetchall("SELECT cum_pnl_micro AS c FROM subscriptions WHERE id = CAST(:s AS uuid)",
                                             {"s": sub}), [{"c": usd("49.8")}])
        self.ledger_ok()

    # ============================================================================================== L4 / L5 / M7
    def test_l4_l5_tamper_detected(self) -> None:
        u = self.user("l4")
        self.deposit(self.api, u, usd(3))
        out = self.psql_script(f"""
            BEGIN;
            SET LOCAL session_replication_role = replica;
            UPDATE ledger_accounts SET owner_user_id = NULL WHERE code = 'platform:revenue:posts';
            UPDATE ledger_accounts SET owner_user_id = '{u}' WHERE code = 'platform:revenue:plans';
            SELECT chain || '|' || reason FROM verify_chain();
            ROLLBACK;""")
        self.assertIn("ledger_accounts|row hash mismatch (account attributes altered)", out)
        out = self.psql_script(f"""
            BEGIN;
            UPDATE ledger_account_balances b SET balance_micro = balance_micro - 1000000000
              FROM ledger_accounts a WHERE a.id = b.account_id AND a.code = 'user:{u}:fee_balance';
            SELECT chain || '|' || reason FROM verify_chain();
            ROLLBACK;""")
        self.assertIn(f"ledger_account_balances|running balance of user:{u}:fee_balance", out)
        # app roles cannot touch the running balances at all
        self.assert_sqlstate("42501", self.api.fetchall, "UPDATE ledger_account_balances SET balance_micro = 0")
        self.assert_sqlstate("42501", self.exe.fetchall, "UPDATE ledger_account_balances SET balance_micro = 0")
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM ledger_balance_mismatches()"), [{"n": 0}])
        self.ledger_ok()

    def test_m7_verify_chain_job_anchors_and_detects_truncation(self) -> None:
        from dataclasses import replace

        from app.config import get_settings
        from app.execution import jobs

        published: list[str] = []

        class Pub:
            def publish(self_inner, text: str) -> dict:
                published.append(text)
                return {"telegram": True}

        alerts = FakeAlerts()
        rt = jobs.Runtime(replace(get_settings(), env="test"), anchor_publisher=Pub(),
                          alert_sink_factory=lambda db: alerts)
        self.deposit(self.api, self.user("m7"), usd(1))
        now = datetime.now(UTC)
        out = jobs.verify_chain(db=self.exe, now=now, runtime=rt)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["anchors_stored"], len(out["heads"]))
        self.assertTrue({"ledger_accounts", "ledger_transactions"} <= {h["chain"] for h in out["heads"]})
        head = {h["chain"]: h for h in out["heads"]}["ledger_transactions"]
        self.assertIn(head["hash"], published[0])
        self.assertIn("ledger_chain_anchor", alerts.kinds())                     # ops email copy
        again = jobs.verify_chain(db=self.exe, now=now, runtime=rt)
        self.assertEqual(again["anchors_stored"], 0)                               # one anchor per day
        # the table owner deletes the newest ledger rows after anchoring: detected against the anchor
        out = self.psql_script(f"""
            BEGIN;
            SET LOCAL session_replication_role = replica;
            DELETE FROM ledger_tx_authorizations WHERE tx_id IN (SELECT id FROM ledger_transactions WHERE seq >= {head['seq']});
            DELETE FROM ledger_entries WHERE tx_id IN (SELECT id FROM ledger_transactions WHERE seq >= {head['seq']});
            DELETE FROM ledger_transactions WHERE seq >= {head['seq']};
            SELECT count(*) FROM verify_chain() WHERE chain = 'ledger_transactions';
            SELECT chain || '|' || reason FROM verify_chain_anchors();
            ROLLBACK;""")
        self.assertIn("ledger_transactions|anchored row missing (chain truncated or rewritten)", out)
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM verify_chain_anchors()"), [{"n": 0}])
        # a broken chain → critical ops alert
        self.psql_script("""
            BEGIN;
            SET LOCAL session_replication_role = replica;
            UPDATE ledger_accounts SET non_negative = NOT non_negative WHERE code = 'stripe:clearing';
            COMMIT;""")
        try:
            bad = jobs.verify_chain(db=self.exe, now=now + timedelta(days=1), runtime=rt)
            self.assertFalse(bad["ok"])
            self.assertIn("ledger_chain_broken", alerts.kinds())
        finally:
            self.psql_script("""
                BEGIN;
                SET LOCAL session_replication_role = replica;
                UPDATE ledger_accounts SET non_negative = NOT non_negative WHERE code = 'stripe:clearing';
                COMMIT;""")
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM verify_chain()"), [{"n": 0}])

    def test_m7_job_is_scheduled(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertIn('"verify-chain": (("app.execution.jobs", "verify_chain"),)',
                      (root / "backend/app/api/adapters.py").read_text())
        self.assertIn('@router.post("/verify-chain"', (root / "backend/app/api/routers/internal.py").read_text())
        self.assertIn('"verify-chain|40 3 * * *|verify-chain|900s|3"', (root / "infra/gcp/env.sh").read_text())


if __name__ == "__main__":
    unittest.main()
