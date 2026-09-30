"""Tests for app.ledger (stdlib unittest; also runs under pytest).

* ``InMemoryLedger*`` tests always run (no DB).
* ``PostgresLedger*`` tests run against a real, migrated database when ``AIJALON_TEST_DATABASE_URL`` is set and
  ``psql`` is on PATH (backend/tests/db/run_db_tests.sh sets it up). They also prove the Python and SQL hash /
  digest implementations agree.
"""
from __future__ import annotations

import os
import sys
import unittest
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

from app.errors import Conflict, InsufficientBalance, NotFound, ValidationFailed  # noqa: E402
from app.ledger import service as ledger  # noqa: E402
from app.ledger.memory import InMemoryLedgerStore  # noqa: E402
from app.money import usd  # noqa: E402

DB_URL = os.environ.get("AIJALON_TEST_DATABASE_URL", "")


@dataclass(frozen=True)
class Line:  # shape of app.execution.ports.LedgerLine
    account_code: str
    amount_micro: int


class LedgerContract:
    """Behaviour every LedgerStore must share. Subclasses provide make_store() and new_user()."""

    def make_store(self):  # pragma: no cover - abstract
        raise NotImplementedError

    def new_user(self) -> str:
        return str(uuid.uuid4())

    def setUp(self):
        self.store = self.make_store()
        self.u = self.new_user()
        self.c = self.new_user()
        self.fee = ledger.fee_balance_account(self.u)
        self.payable = ledger.creator_payable_account(self.c)

    def key(self, name: str) -> str:
        return f"test:{name}:{uuid.uuid4()}"

    def deposit(self, amount: int, key: str | None = None):
        return ledger.post_transaction(self.store, key or self.key("dep"), "deposit", "top-up",
                                       [(ledger.TREASURY_HL_USDC, amount), (self.fee, -amount)], "test")

    # ------------------------------------------------------------------ basics
    def test_deposit_and_balances(self):
        tx = self.deposit(usd(10))
        self.assertTrue(tx.created)
        self.assertEqual(ledger.get_balance(self.store, self.fee), -usd(10))            # liability: credit
        self.assertEqual(ledger.available_fee_balance(self.store, self.u), usd(10))
        self.assertEqual(ledger.normal_balance(self.store, self.fee), usd(10))
        acct = self.store.get_account(self.fee)
        self.assertEqual((acct.kind, acct.owner_user_id, acct.non_negative), ("liability", self.u, True))

    def test_subscription_split_charge(self):
        self.deposit(usd(20))
        tx = ledger.post_transaction(self.store, self.key("sub"), "subscription_renewal", None,
                                     [(self.fee, usd(20)), (self.payable, -19_400_000),
                                      (ledger.PLATFORM_REVENUE_SUBSCRIPTION, -600_000)], "test")
        self.assertEqual(len(tx.entries), 3)
        self.assertEqual(ledger.available_fee_balance(self.store, self.u), 0)
        self.assertEqual(ledger.normal_balance(self.store, self.payable), 19_400_000)

    def test_missing_account_balance_is_zero(self):
        self.assertEqual(ledger.available_fee_balance(self.store, self.new_user()), 0)
        with self.assertRaises(NotFound):
            ledger.get_balance(self.store, ledger.fee_balance_account(self.new_user()), missing_ok=False)

    # ------------------------------------------------------------------ validation
    def test_rejects_unbalanced_and_malformed(self):
        bad = [
            [(ledger.TREASURY_HL_USDC, 5), (self.fee, -4)],             # unbalanced
            [(ledger.TREASURY_HL_USDC, 0), (self.fee, 0)],              # zero amounts
            [(ledger.TREASURY_HL_USDC, 5)],                             # single entry
            [],                                                         # empty
            [(ledger.TREASURY_HL_USDC, 1.5), (self.fee, -1.5)],         # float
            [(ledger.TREASURY_HL_USDC, True), (self.fee, -1)],          # bool
            [("Bad Code", 5), (self.fee, -5)],                          # bad code
        ]
        for entries in bad:
            with self.subTest(entries=entries), self.assertRaises(ValidationFailed):
                ledger.post_transaction(self.store, self.key("bad"), "deposit", None, entries, "test")
        with self.assertRaises(ValidationFailed):
            ledger.post_transaction(self.store, self.key("bad"), "Bad Kind", None,
                                    [(ledger.TREASURY_HL_USDC, 1), (self.fee, -1)], "test")

    def test_unknown_platform_account(self):
        with self.assertRaises(NotFound):
            ledger.post_transaction(self.store, self.key("x"), "deposit", None,
                                    [("platform:nope", 1), (ledger.TREASURY_HL_USDC, -1)], "test")

    # ------------------------------------------------------------------ idempotency
    def test_idempotent_replay_and_conflict(self):
        k = self.key("idem")
        a = self.deposit(usd(10), key=k)
        b = ledger.post_transaction(self.store, k, "deposit", "different memo is fine",
                                    [(self.fee, -usd(10)), (ledger.TREASURY_HL_USDC, usd(10))], "other")
        self.assertEqual(a.id, b.id)
        self.assertTrue(a.created)
        self.assertFalse(b.created)
        self.assertEqual(ledger.available_fee_balance(self.store, self.u), usd(10))    # not doubled
        with self.assertRaises(Conflict):
            self.deposit(usd(11), key=k)
        with self.assertRaises(Conflict):
            ledger.post_transaction(self.store, k, "stripe_topup", None,
                                    [(ledger.TREASURY_HL_USDC, usd(10)), (self.fee, -usd(10))], "test")

    def test_store_level_idempotency_under_race(self):
        """Second writer skipped the service pre-check (race): the store itself must still be idempotent."""
        k = self.key("race")
        a = self.deposit(usd(3), key=k)
        b = self.store.insert_tx(k, "deposit", None, "t", [(ledger.TREASURY_HL_USDC, usd(3)), (self.fee, -usd(3))])
        self.assertEqual((a.id, b.created), (b.id, False))
        with self.assertRaises(Conflict):
            self.store.insert_tx(k, "deposit", None, "t", [(ledger.TREASURY_HL_USDC, usd(4)), (self.fee, -usd(4))])

    # ------------------------------------------------------------------ non-negative accounts
    def test_fee_balance_cannot_go_negative(self):
        self.deposit(usd(10))
        with self.assertRaises(InsufficientBalance):
            ledger.post_transaction(self.store, self.key("sub"), "subscription_renewal", None,
                                    [(self.fee, usd(20)), (ledger.PLATFORM_REVENUE_SUBSCRIPTION, -usd(20))], "test")
        self.assertEqual(ledger.available_fee_balance(self.store, self.u), usd(10))

    def test_store_rejects_overdraft_without_service_precheck(self):
        self.deposit(usd(1))
        with self.assertRaises(InsufficientBalance):
            self.store.insert_tx(self.key("od"), "post_purchase", None, "t",
                                 [(self.fee, usd(2)), (ledger.PLATFORM_REVENUE_POSTS, -usd(2))])

    def test_overdraft_kinds_and_topup_on_debt(self):
        self.deposit(usd(10))
        # REVIEW_MONEY C1 (0010): only the collected $10 may reach revenue; the uncollected $5 goes to ps_pending
        with self.assertRaises(InsufficientBalance):
            ledger.post_transaction(self.store, self.key("ps0"), "profit_share", None,
                                    [(self.fee, usd(15)), (ledger.PLATFORM_REVENUE_PROFIT_SHARE, -usd(15))], "test")
        ledger.post_transaction(self.store, self.key("ps"), "profit_share", None,
                                [(self.fee, usd(15)), (ledger.PLATFORM_REVENUE_PROFIT_SHARE, -usd(10)),
                                 (ledger.ps_pending_account(self.u, None), -usd(5))], "test")
        self.assertEqual(ledger.available_fee_balance(self.store, self.u), -usd(5))   # debt
        self.deposit(usd(2))                                                          # top-up onto debt is fine
        self.assertEqual(ledger.available_fee_balance(self.store, self.u), -usd(3))
        with self.assertRaises(InsufficientBalance):                                  # still cannot spend
            ledger.post_transaction(self.store, self.key("post"), "post_purchase", None,
                                    [(self.fee, 1), (ledger.PLATFORM_REVENUE_POSTS, -1)], "test")

    def test_creator_payable_cannot_be_overpaid(self):
        self.deposit(usd(20))
        ledger.post_transaction(self.store, self.key("sub"), "subscription_renewal", None,
                                [(self.fee, usd(20)), (self.payable, -usd(19)),
                                 (ledger.PLATFORM_REVENUE_SUBSCRIPTION, -usd(1))], "test")
        with self.assertRaises(InsufficientBalance):
            ledger.post_transaction(self.store, self.key("payout"), "payout", None,
                                    [(self.payable, usd(20)), (ledger.TREASURY_HL_USDC, -usd(20))], "test")
        ledger.post_transaction(self.store, self.key("payout"), "payout", None,
                                [(self.payable, usd(19)), (ledger.TREASURY_HL_USDC, -usd(19))], "test")
        self.assertEqual(ledger.normal_balance(self.store, self.payable), 0)

    # ------------------------------------------------------------------ accounts
    def test_ensure_account(self):
        a = ledger.ensure_account(self.store, "referrer:" + self.u + ":payable", "liability", self.u)
        self.assertTrue(a.non_negative)
        self.assertEqual(ledger.ensure_account(self.store, a.code, "liability", self.u).id, a.id)
        with self.assertRaises(Conflict):
            ledger.ensure_account(self.store, a.code, "asset")
        with self.assertRaises(ValidationFailed):
            ledger.ensure_account(self.store, "x:y", "equity")
        with self.assertRaises(ValidationFailed):
            ledger.ensure_account(self.store, ledger.fee_balance_account(self.u), "liability", self.u,
                                  non_negative=False)

    # ------------------------------------------------------------------ adapters + hashes
    def test_port_adapters(self):
        bound = ledger.BoundLedger(self.store)
        k = self.key("bound")
        tx_id, created = bound.post_transaction(idempotency_key=k, kind="deposit", memo="m", created_by="t",
                                                lines=[Line(ledger.TREASURY_HL_USDC, 7), Line(self.fee, -7)])
        self.assertTrue(created)
        self.assertEqual(bound.post_transaction(idempotency_key=k, kind="deposit", memo="m", created_by="t",
                                                lines=[Line(self.fee, -7), Line(ledger.TREASURY_HL_USDC, 7)]),
                         (tx_id, False))
        self.assertTrue(bound.has_transaction(k))
        self.assertFalse(bound.has_transaction(self.key("nope")))
        self.assertEqual(bound.balance(self.fee), -7)
        svc = ledger.LedgerService()
        tid = svc.post(self.store, idempotency_key=self.key("svc"), kind="deposit", memo="",
                       entries=[(ledger.TREASURY_HL_USDC, 3), (self.fee, -3)], created_by="t")
        self.assertIsInstance(tid, str)
        self.assertEqual(svc.balance(self.store, self.fee), -10)

    def test_hash_chain_links_and_python_twin(self):
        a = self.deposit(5)
        b = self.deposit(6)
        self.assertEqual(b.seq, a.seq + 1)
        self.assertEqual(b.prev_hash, a.hash)
        for tx in (a, b):
            self.assertEqual(tx.entries_digest, ledger.entries_digest(tx.entries))
            self.assertEqual(tx.hash, ledger.tx_hash(tx.prev_hash, id=tx.id, idempotency_key=tx.idempotency_key,
                                                     kind=tx.kind, memo=tx.memo, created_by=tx.created_by,
                                                     created_at=tx.created_at, entries_digest=tx.entries_digest))


class PureHelpersTest(unittest.TestCase):
    def test_digest_is_order_independent(self):
        e1 = [("b:x", -5), ("a:y", 3), ("a:y", 2)]
        self.assertEqual(ledger.entries_digest(e1), ledger.entries_digest(list(reversed(e1))))
        self.assertNotEqual(ledger.entries_digest(e1), ledger.entries_digest([("b:x", -5), ("a:y", 5)]))

    def test_default_account_specs(self):
        u = str(uuid.uuid4())
        self.assertEqual(ledger.default_account_spec(ledger.fee_balance_account(u)), ("liability", u, True))
        self.assertIsNone(ledger.default_account_spec("platform:revenue:builder"))
        self.assertIsNone(ledger.default_account_spec("user:not-a-uuid:fee_balance"))


class InMemoryLedgerTest(LedgerContract, unittest.TestCase):
    def make_store(self):
        return InMemoryLedgerStore()

    def test_tamper_detection(self):
        self.deposit(5)
        self.deposit(6)
        self.assertIsNone(self.store.verify_chain())
        from dataclasses import replace

        self.store.txs[0] = replace(self.store.txs[0], memo="edited")
        self.assertEqual(self.store.verify_chain(), 1)

    def test_clock_is_used(self):
        t0 = datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc)
        store = InMemoryLedgerStore(clock=lambda: t0 + timedelta(microseconds=1))
        u = str(uuid.uuid4())
        tx = ledger.post_transaction(store, "k", "deposit", None,
                                     [(ledger.TREASURY_HL_USDC, 1), (ledger.fee_balance_account(u), -1)], "t")
        self.assertEqual(tx.created_at, "2026-09-30T00:30:00.000001+00:00")


@unittest.skipUnless(DB_URL, "set AIJALON_TEST_DATABASE_URL (see backend/tests/db/run_db_tests.sh)")
class PostgresLedgerTest(LedgerContract, unittest.TestCase):
    def make_store(self):
        from app.db.repositories.ledger import PostgresLedgerStore
        from psql_runner import PsqlRunner, psql_available

        if not psql_available():
            self.skipTest("psql not on PATH")
        self.runner = PsqlRunner(DB_URL)
        return PostgresLedgerStore(self.runner)

    def new_user(self) -> str:
        uid = str(uuid.uuid4())
        self.runner.fetchall("INSERT INTO users (id, firebase_uid) VALUES (CAST(:id AS uuid), :fb)",
                             {"id": uid, "fb": "fb-" + uid})
        return uid

    def test_sql_and_python_hashes_agree_with_verify_chain(self):
        tx = self.deposit(12_345, key=self.key("parity"))
        rows = self.runner.fetchall("SELECT count(*) AS n FROM verify_chain()")
        self.assertEqual(rows[0]["n"], 0)
        self.assertEqual(tx.entries_digest, ledger.entries_digest(tx.entries))
        self.assertEqual(tx.hash, ledger.tx_hash(tx.prev_hash, id=tx.id, idempotency_key=tx.idempotency_key,
                                                 kind=tx.kind, memo=tx.memo, created_by=tx.created_by,
                                                 created_at=tx.created_at, entries_digest=tx.entries_digest))
        sql_digest = self.runner.fetchall(
            "SELECT ledger_entries_digest(CAST(:codes AS text[]), CAST(:amts AS bigint[])) AS d",
            {"codes": "{" + ",".join(c for c, _ in tx.entries) + "}",
             "amts": "{" + ",".join(str(a) for _, a in tx.entries) + "}"})
        self.assertEqual(sql_digest[0]["d"], tx.entries_digest)

    def test_history(self):
        from app.db.repositories.ledger import PostgresLedgerStore

        self.deposit(1)
        self.deposit(2)
        rows = PostgresLedgerStore(self.runner).history(self.fee, limit=10)
        self.assertEqual([r["amount_micro"] for r in rows], [-2, -1])


if __name__ == "__main__":
    unittest.main()
