"""REVIEW_MONEY M5 (remaining) — ledger posting lockdown (migrations/0015_ledger_lockdown.sql) — and the M7(b)/(c)
reconcile bookings, against a REAL PostgreSQL 16 (throwaway database, every migration applied; harness from
tests/test_integration_exec_db.py), running as the real roles (SET ROLE app_api / app_executor).

- Every posting the app makes today, with its real key shape, entry shape and role, matches a rule and records the
  authorisation (role, invoking role, rule id); the uncollected-profit-share release posts as 'system'.
- Each role is refused the other role's kinds (AJ403 → ValidationFailed), cannot claim another role, cannot INSERT into
  the ledger tables nor call the owner-only functions.
- Reconcile books builder-reward claims and Stripe payouts through PgLedger as app_executor, idempotently, with its
  cursors in job_cursors; the receivable check reads the unrecognised builder fees from the fills table.

Skipped without psql / a reachable cluster (AIJALON_TEST_PG_ADMIN_URL, default postgresql://postgres@localhost:55432).
"""
from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import FakeAlerts  # noqa: E402
from test_integration_exec_db import RUN, LiveDb, RoleRunner  # noqa: E402

from app.db.engine import DbError  # noqa: E402
from app.errors import ValidationFailed  # noqa: E402

USD = 1_000_000
UTC = timezone.utc

# (role, kind) of every ledger posting the application makes (grep: app/api/ledger_ops.py, app/api/billing_ops.py,
# app/payments/{stripe_pay,usdc}.py via ledger_ops.apply_credit/apply_debit, app/execution/settlement.py,
# app/jobs_data/deposits.py, app/execution/reconcile.py; ps_pending_release from the SQL function).
APP_POSTINGS = {
    ("app_api", "subscription_start"), ("app_api", "subscription_renewal"), ("app_api", "plan_purchase"),
    ("app_api", "post_purchase"), ("app_api", "withdrawal_hold"), ("app_api", "withdrawal_release"),
    ("app_api", "withdrawal_sent"), ("app_api", "payout_hold"), ("app_api", "payout_release"),
    ("app_api", "payout_sent"), ("app_api", "deposit"), ("app_api", "stripe_refund"), ("app_api", "stripe_dispute"),
    ("app_api", "stripe_dispute_reinstated"), ("app_api", "suspense_release"), ("app_api", "suspense_refund"),
    ("app_api", "suspense_refund_sent"),
    ("app_executor", "profit_share"), ("app_executor", "subscription_renewal"), ("app_executor", "plan_renewal"),
    ("app_executor", "builder_fee"), ("app_executor", "deposit"), ("app_executor", "deposit_held"),
    ("app_executor", "builder_rewards_claim"), ("app_executor", "stripe_payout"),
    ("app_executor", "stripe_payout_reversal"),
    ("system", "ps_pending_release"),
}


def _h() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex


@unittest.skipUnless(RUN, "needs psql and a reachable PostgreSQL (AIJALON_TEST_PG_ADMIN_URL)")
class LedgerLockdownDbTest(unittest.TestCase):
    db_: LiveDb

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_ = LiveDb()
        cls.db_.create()
        if "0015_ledger_lockdown.sql" not in cls.db_.applied:
            cls.db_.drop()
            raise AssertionError(f"0015_ledger_lockdown.sql did not apply: {cls.db_.note}")
        cls.admin = RoleRunner(cls.db_.url, role=None)
        cls.exe = RoleRunner(cls.db_.url, role="app_executor")
        cls.api = RoleRunner(cls.db_.url, role="app_api")
        cls.admin.fetchall("""INSERT INTO ledger_accounts (code, kind, non_negative) VALUES
                              ('payouts:pending', 'liability', true), ('withdrawals:pending', 'liability', true)
                              ON CONFLICT (code) DO NOTHING""")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db_.drop()

    def user(self, tag: str) -> str:
        t = f"{tag}{uuid.uuid4().hex[:8]}"
        return self.admin.fetchall("""INSERT INTO users (firebase_uid, email, referral_code) VALUES (:u, :e, :c)
                                      RETURNING id::text AS id""", {"u": "fb" + t, "e": f"{t}@x.test", "c": "R" + t})[0]["id"]

    def post(self, runner: RoleRunner, key: str, kind: str, entries: list[tuple[str, int]], *, role: str | None = None):
        from app.db.repositories.ledger import PostgresLedgerStore
        from app.ledger.service import post_transaction

        return post_transaction(PostgresLedgerStore(runner, role=role), key, kind, "test", entries, "tests")

    def refused(self, runner: RoleRunner, key: str, kind: str, entries: list[tuple[str, int]]) -> None:
        """Straight to ledger_post_as (no Python balance pre-check): the DB rule check must refuse it (AJ403)."""
        from app.db.repositories.ledger import PostgresLedgerStore
        from app.ledger.service import normalize_entries

        with self.assertRaises(ValidationFailed) as cm:
            PostgresLedgerStore(runner).insert_tx(key, kind, "test", "tests", normalize_entries(entries))
        self.assertEqual(cm.exception.details.get("sqlstate"), "AJ403", cm.exception.message)

    def auth(self, key: str) -> dict:
        rows = self.admin.fetchall("""SELECT z.authorized_as, z.invoker, z.rule_id, r.kind AS rule_kind
                                        FROM ledger_tx_authorizations z JOIN ledger_transactions t ON t.id = z.tx_id
                                        LEFT JOIN ledger_posting_rules r ON r.id = z.rule_id
                                       WHERE t.idempotency_key = :k""", {"k": key})
        self.assertEqual(len(rows), 1, key)
        return rows[0]

    def ledger_ok(self) -> None:
        self.assertEqual(self.admin.fetchall("SELECT chain, seq, reason FROM verify_chain()"), [])
        self.assertEqual(self.admin.fetchall("SELECT coalesce(sum(amount_micro), 0)::bigint AS s FROM ledger_entries"),
                         [{"s": 0}])

    # ------------------------------------------------------------------------------------------------ rules
    def test_rule_table_covers_every_app_posting(self) -> None:
        rules = {(r["role"], r["kind"]) for r in self.api.fetchall("SELECT role, kind FROM ledger_posting_rules")}
        self.assertEqual(APP_POSTINGS - rules, set())
        self.assertEqual(rules - APP_POSTINGS, {("app_migrator", "*")})

    def test_every_app_posting_goes_through_its_rule(self) -> None:
        from app.api.billing_ops import renewal_key as api_renewal_key
        from app.api.ledger_ops import suspense_release_key
        from app.execution.settlement import builder_fee_key, plan_key, profit_share_key, renewal_key

        u, c, r, u2 = self.user("lu"), self.user("lc"), self.user("lr"), self.user("lu2")
        fee, fee2 = f"user:{u}:fee_balance", f"user:{u2}:fee_balance"
        cpay, rpay = f"creator:{c}:payable", f"referrer:{r}:payable"
        sub, post_id, w1, w2, p1, p2 = (str(uuid.uuid4()) for _ in range(6))
        end = datetime(2026, 10, 1, tzinfo=UTC)
        h1, h2, h3 = _h(), _h(), _h()
        posted: list[tuple[str, str, str]] = []            # (key, expected role, kind)

        def ok(runner: RoleRunner, role: str, key: str, kind: str, entries: list[tuple[str, int]]) -> None:
            tx = self.post(runner, key, kind, entries)
            self.assertTrue(tx.created, key)
            posted.append((key, role, kind))

        A, E = self.api, self.exe
        # money in
        ok(A, "app_api", f"usdc_hl:{_h()}", "deposit", [("treasury:hl_usdc", 1000 * USD), (fee, -1000 * USD)])
        ok(A, "app_api", "stripe:pi_" + uuid.uuid4().hex[:16], "deposit", [("stripe:clearing", 100 * USD), (fee, -100 * USD)])
        ok(E, "app_executor", f"usdc_hl:{_h()}", "deposit", [("treasury:hl_usdc", 50 * USD), (fee, -50 * USD)])
        ok(E, "app_executor", f"usdc_hl:{h1}", "deposit_held", [("treasury:hl_usdc", 7 * USD), ("suspense:usdc_unattributed", -7 * USD)])
        ok(E, "app_executor", f"usdc_hl:{h2}", "deposit_held", [("treasury:hl_usdc", 3 * USD), ("suspense:usdc_unattributed", -3 * USD)])
        ok(A, "app_api", suspense_release_key(h1), "suspense_release", [("suspense:usdc_unattributed", 7 * USD), (fee, -7 * USD)])
        ok(A, "app_api", suspense_release_key(h2), "suspense_refund", [("suspense:usdc_unattributed", 3 * USD), ("refunds:usdc_pending", -3 * USD)])
        ok(A, "app_api", f"suspense_refund:{h2}:sent", "suspense_refund_sent", [("refunds:usdc_pending", 3 * USD), ("treasury:hl_usdc", -3 * USD)])
        # API charges
        ok(A, "app_api", f"sub:{sub}:start", "subscription_start", [(fee, 30 * USD), (cpay, -29 * USD), ("platform:revenue:subscription", -1 * USD)])
        ok(A, "app_api", api_renewal_key(sub, end), "subscription_renewal", [(fee, 30 * USD), (cpay, -29 * USD), ("platform:revenue:subscription", -1 * USD)])
        ok(A, "app_api", f"plan:{u}:start:max:2026-10-01", "plan_purchase", [(fee, 50 * USD), ("platform:revenue:plans", -50 * USD)])
        ok(A, "app_api", f"post:{post_id}:{u}", "post_purchase", [(fee, 5 * USD), (cpay, -4 * USD), ("platform:revenue:posts", -1 * USD)])
        # withdrawals / payouts (hold → release, hold → sent)
        ok(A, "app_api", f"withdrawal:{w1}:hold", "withdrawal_hold", [(fee, 20 * USD), ("withdrawals:pending", -20 * USD)])
        ok(A, "app_api", f"withdrawal:{w1}:release", "withdrawal_release", [("withdrawals:pending", 20 * USD), (fee, -20 * USD)])
        ok(A, "app_api", f"withdrawal:{w2}:hold", "withdrawal_hold", [(fee, 10 * USD), ("withdrawals:pending", -10 * USD)])
        ok(A, "app_api", f"withdrawal:{w2}:sent", "withdrawal_sent", [("withdrawals:pending", 10 * USD), ("treasury:hl_usdc", -10 * USD)])
        ok(A, "app_api", f"payout:{p1}:hold", "payout_hold", [(cpay, 10 * USD), ("payouts:pending", -10 * USD)])
        ok(A, "app_api", f"payout:{p1}:release", "payout_release", [("payouts:pending", 10 * USD), (cpay, -10 * USD)])
        ok(A, "app_api", f"payout:{p2}:hold", "payout_hold", [(cpay, 5 * USD), ("payouts:pending", -5 * USD)])
        ok(A, "app_api", f"payout:{p2}:sent", "payout_sent", [("payouts:pending", 5 * USD), ("treasury:hl_usdc", -5 * USD)])
        # Stripe reversals
        ok(A, "app_api", f"stripe:refund:ch_{uuid.uuid4().hex[:10]}:500", "stripe_refund", [(fee, 5 * USD), ("stripe:clearing", -5 * USD)])
        dp = "dp_" + uuid.uuid4().hex[:10]
        ok(A, "app_api", f"stripe:dispute:{dp}", "stripe_dispute", [(fee, 3 * USD), ("stripe:clearing", -3 * USD)])
        ok(A, "app_api", f"stripe:dispute_reinstated:{dp}", "stripe_dispute_reinstated", [("stripe:clearing", 3 * USD), (fee, -3 * USD)])
        # settlement
        ok(E, "app_executor", profit_share_key(sub, end.date()), "profit_share",
           [(fee, 20 * USD), (cpay, -18 * USD), ("platform:revenue:profit_share", -2 * USD)])
        ok(E, "app_executor", renewal_key(str(uuid.uuid4()), end), "subscription_renewal", [(fee, 30 * USD), ("platform:revenue:subscription", -30 * USD)])
        ok(E, "app_executor", plan_key(u, end), "plan_renewal", [(fee, 50 * USD), ("platform:revenue:plans", -50 * USD)])
        ok(E, "app_executor", builder_fee_key("0x" + "aa" * 20, "123"), "builder_fee",
           [("builder:hl_receivable", 10 * USD), ("platform:revenue:builder", -5 * USD), (cpay, -3 * USD), (rpay, -2 * USD)])
        # reconcile (M7)
        ok(E, "app_executor", f"builder_claim:{h3}:1700000000000", "builder_rewards_claim",
           [("treasury:hl_usdc", 10 * USD), ("builder:hl_receivable", -10 * USD)])
        ok(E, "app_executor", "stripe:payout:txn_" + uuid.uuid4().hex[:12], "stripe_payout",
           [("bank:payouts", 50 * USD), ("expense:stripe_fees", 1 * USD), ("stripe:clearing", -51 * USD)])
        ok(E, "app_executor", "stripe:payout_reversal:txn_" + uuid.uuid4().hex[:12], "stripe_payout_reversal",
           [("stripe:clearing", 5 * USD), ("bank:payouts", -5 * USD)])
        # uncollected profit share (u2 has $0) → pending; a top-up by the API releases it as 'system'
        self.admin.fetchall("INSERT INTO ledger_accounts (code, kind) VALUES (:a, 'liability'), (:b, 'liability')",
                            {"a": f"ps_pending:{u2}:{c}", "b": f"ps_pending:{u2}:platform"})
        ok(E, "app_executor", profit_share_key(str(uuid.uuid4()), end.date()), "profit_share",
           [(fee2, 12 * USD), (f"ps_pending:{u2}:{c}", -10 * USD), (f"ps_pending:{u2}:platform", -2 * USD)])
        ok(A, "app_api", f"usdc_hl:{_h()}", "deposit", [("treasury:hl_usdc", 100 * USD), (fee2, -100 * USD)])
        rel = self.admin.fetchall("SELECT idempotency_key AS k FROM ledger_transactions WHERE idempotency_key LIKE :p",
                                  {"p": f"ps_release:{u2}:%"})
        self.assertEqual(len(rel), 1)
        posted.append((rel[0]["k"], "system", "ps_pending_release"))

        seen: set[tuple[str, str]] = set()
        for key, role, kind in posted:
            a = self.auth(key)
            self.assertEqual((a["authorized_as"], a["rule_kind"]), (role, kind), key)
            self.assertEqual(a["invoker"], role if role != "system" else "app_api", key)
            seen.add((role, kind))
        self.assertEqual(seen, APP_POSTINGS)
        self.assertEqual(-self.admin.fetchall("SELECT ledger_raw_balance(:c) AS b", {"c": cpay})[0]["b"],
                         (29 + 29 + 4 - 10 + 10 - 5 + 18 + 3 + 10) * USD)   # … + profit share, builder fee, release
        self.ledger_ok()

    def test_roles_cannot_post_each_others_kinds(self) -> None:
        u, c = self.user("xu"), self.user("xc")
        fee, cpay = f"user:{u}:fee_balance", f"creator:{c}:payable"
        self.post(self.api, f"usdc_hl:{_h()}", "deposit", [("treasury:hl_usdc", 100 * USD), (fee, -100 * USD)])
        api_refused = [
            ("profit_share", [(fee, USD), ("platform:revenue:profit_share", -USD)]),
            ("plan_renewal", [(fee, USD), ("platform:revenue:plans", -USD)]),
            ("builder_fee", [("builder:hl_receivable", USD), ("platform:revenue:builder", -USD)]),
            ("deposit_held", [("treasury:hl_usdc", USD), ("suspense:usdc_unattributed", -USD)]),
            ("builder_rewards_claim", [("treasury:hl_usdc", USD), ("builder:hl_receivable", -USD)]),
            ("stripe_payout", [("bank:payouts", USD), ("stripe:clearing", -USD)]),
            ("ps_pending_release", [(f"ps_pending:{u}:platform", USD), ("platform:revenue:profit_share", -USD)]),
            ("adjustment", [("treasury:hl_usdc", USD), (fee, -USD)]),
            ("deposit", [("treasury:hl_usdc", USD), (cpay, -USD)]),                   # right kind, wrong account
            ("withdrawal_hold", [(fee, USD), ("treasury:hl_usdc", -USD)]),            # right kind, wrong account
            ("deposit", [("stripe:clearing", USD), (fee, -USD)]),                     # clearing needs a stripe: key
        ]
        for kind, entries in api_refused:
            key = ("stripe:x" if kind == "builder_rewards_claim" else "k") + uuid.uuid4().hex
            with self.subTest(role="app_api", kind=kind):
                self.refused(self.api, key, kind, entries)
        exe_refused = [
            ("subscription_start", [(fee, USD), ("platform:revenue:subscription", -USD)]),
            ("post_purchase", [(fee, USD), ("platform:revenue:posts", -USD)]),
            ("withdrawal_hold", [(fee, USD), ("withdrawals:pending", -USD)]),
            ("payout_sent", [("payouts:pending", USD), ("treasury:hl_usdc", -USD)]),
            ("stripe_refund", [(fee, USD), ("stripe:clearing", -USD)]),
            ("suspense_release", [("suspense:usdc_unattributed", USD), (fee, -USD)]),
            ("stripe_payout", [("bank:payouts", USD), ("treasury:hl_usdc", -USD)]),   # right kind, wrong account
        ]
        for kind, entries in exe_refused:
            key = ("stripe:refund:ch_1:" if kind == "stripe_refund" else "stripe:payout:txn_") + uuid.uuid4().hex[:12]
            with self.subTest(role="app_executor", kind=kind):
                self.refused(self.exe, key, kind, entries)
        # claiming the other role is refused (the session is not a member)
        with self.assertRaises(ValidationFailed):
            self.post(self.api, f"k{uuid.uuid4().hex}", "profit_share", [(fee, USD), ("platform:revenue:profit_share", -USD)],
                      role="app_executor")
        with self.assertRaises(ValidationFailed):
            self.post(self.exe, f"k{uuid.uuid4().hex}", "withdrawal_hold", [(fee, USD), ("withdrawals:pending", -USD)],
                      role="app_api")
        with self.assertRaises(ValidationFailed):          # unknown role name, refused in Python before the DB
            from app.db.repositories.ledger import PostgresLedgerStore
            PostgresLedgerStore(self.api, role="system")
        # the tables and the owner-only functions are closed to both roles
        for runner in (self.api, self.exe):
            for sql in ("INSERT INTO ledger_transactions (idempotency_key, kind, created_by, entries_digest) "
                        "VALUES ('d', 'deposit', 't', ledger_entries_digest('{}', '{}'))",
                        "SELECT * FROM ledger_post('d', 'deposit', 'x', 't', CAST('[]' AS jsonb))",
                        "SELECT * FROM ledger_post_core('app_api', 'd', 'deposit', 'x', 't', CAST('[]' AS jsonb))",
                        "INSERT INTO ledger_posting_rules (id, role, kind, key_pattern, debit_pattern, credit_pattern, note) "
                        "VALUES (998, 'app_api', 'x', '^.+$', '^.+$', '^.+$', 'x')",
                        "INSERT INTO ledger_tx_authorizations (tx_id, kind, authorized_as) "
                        "SELECT id, kind, 'app_api' FROM ledger_transactions LIMIT 1"):
                with self.subTest(role=runner.role, sql=sql[:40]), self.assertRaises(DbError) as cm:
                    runner.fetchall(sql)
                self.assertEqual(cm.exception.sqlstate, "42501", str(cm.exception))
        self.ledger_ok()

    # ------------------------------------------------------------------------------------------------ M7 bookings
    def test_reconcile_books_claims_and_payouts_as_executor(self) -> None:
        from test_treasury_books import Const, FakeClaims, FakeStripeReader, Pos, Treasury

        from app.execution.pg import PgDatabase, PgLedger, PgReconcileRepo, PgUnitOfWork
        from app.execution.ports import BuilderClaim, StripePayoutMovement
        from app.execution.reconcile import Reconciler

        pdb = PgDatabase(self.exe)
        repo = PgReconcileRepo(pdb)
        led = PgLedger(pdb)
        receivable0 = led.balance("builder:hl_receivable")
        clearing0 = led.balance("stripe:clearing")
        u = self.user("rc")
        self.post(self.api, "stripe:pi_" + uuid.uuid4().hex[:16], "deposit",
                  [("stripe:clearing", 80 * USD), (f"user:{u}:fee_balance", -80 * USD)])
        h = _h()
        tag = uuid.uuid4().hex[:10]
        claims = FakeClaims([BuilderClaim(f"{h}:1700000000000", 1_700_000_000_000, 4 * USD)],
                            unclaimed=receivable0 - 4 * USD)
        mv = [StripePayoutMovement(f"txn_a{tag}", "po_a", 1_700_000_100_000, 50 * USD, 0),
              StripePayoutMovement(f"txn_b{tag}", "po_b", 1_700_000_200_000, 10 * USD, 1 * USD),
              StripePayoutMovement(f"txn_c{tag}", "po_a", 1_700_000_300_000, 5 * USD, 0, reversal=True)]
        stripe = FakeStripeReader(clearing0 + 80 * USD - 56 * USD, mv)

        def run():
            return Reconciler(repo=repo, positions=Pos(), builder_rewards=Const(0), treasury=Treasury(0), ledger=led,
                              alerts=FakeAlerts(), builder_claims=claims, stripe=stripe, uow=PgUnitOfWork(pdb),
                              solvency=repo).run("2026-10-02")

        rep = run()
        self.assertEqual((rep.builder_claims_booked, rep.stripe_payouts_booked), (1, 3), rep.errors)
        self.assertEqual(rep.errors, [])
        self.assertEqual(led.balance("builder:hl_receivable"), receivable0 - 4 * USD)
        self.assertFalse(rep.builder_receivable_mismatch)
        self.assertEqual(rep.builder_unrecognised_micro, 0)
        self.assertEqual((rep.stripe_clearing_status, rep.stripe_clearing_ledger_micro), ("ok", clearing0 + 24 * USD))
        self.assertEqual(self.auth(f"builder_claim:{h}:1700000000000")["authorized_as"], "app_executor")
        self.assertEqual(self.auth(f"stripe:payout:txn_b{tag}")["rule_kind"], "stripe_payout")
        self.assertEqual({repo.get_cursor("builder_claims"), repo.get_cursor("stripe_payouts")},
                         {1_700_000_000_000, 1_700_000_300_000})
        rep2 = run()                                          # idempotent: nothing new, cursors unchanged
        self.assertEqual((rep2.builder_claims_booked, rep2.stripe_payouts_booked, rep2.errors), (0, 0, []))
        repo.set_cursor("stripe_payouts", 5)                  # never moves back
        self.assertEqual(repo.get_cursor("stripe_payouts"), 1_700_000_300_000)
        self.ledger_ok()


if __name__ == "__main__":
    unittest.main()
