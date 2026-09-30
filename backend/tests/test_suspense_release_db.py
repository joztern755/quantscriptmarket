"""Held USDC release (suspense:usdc_unattributed, maker-checker) against a REAL migrated Postgres (0001–0009).

app/api/suspense.py + app/api/store.py + app/api/ledger_ops.py run AS the `app_api` role (grants proven); the held
transfer itself is booked the way deposits-scan does it (ledger kind deposit_held + usdc_held_deposits row, as
`app_executor`). Covers: attribute (verified sender only, fee balance + withdrawable deposits row + user alert),
refund (suspense → refunds:usdc_pending → treasury on the recorded tx hash), four-eyes / no self-approval /
no self-attribution, one live release per transfer, rejection then re-proposal, legacy transfers without a recorded
sender (on-chain verified sender required), ledger idempotency, the DB guard trigger and CHECKs, audit rows.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api_store_db import DB_URL, RUN  # noqa: E402

if RUN:
    from test_api_store_db import ApiRoleRunner, _Svc  # noqa: E402

from app.errors import Conflict, Forbidden, NotFound, ValidationFailed  # noqa: E402

TREASURY = "0x" + "7e" * 20
SUSPENSE = "suspense:usdc_unattributed"
REFUNDS = "refunds:usdc_pending"


def _addr(seed: str) -> str:
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def _hash(seed: str) -> str:
    return "0x" + hashlib.sha256(("tx" + seed).encode()).hexdigest()


class Notifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def notify(self, conn: Any, *, user_id, severity, kind, payload, dedup_key=None) -> None:
        self.sent.append({"user_id": user_id, "severity": severity, "kind": kind, "payload": dict(payload)})


class Audit:
    def __init__(self, store: Any) -> None:
        self.store = store

    def write(self, conn: Any, *, actor, action, target, payload, ip_hash) -> None:
        self.store.insert_audit(conn, actor=actor, action=action, target=target, payload=payload, ip_hash=ip_hash)


class TypedData:
    def usd_send(self, *, destination: str, amount: str, time_ms: int, signature_chain_id: str) -> dict:
        from app.hl.typed_data import usd_send_request

        return usd_send_request(destination, amount, time_ms=time_ms, signature_chain_id=signature_chain_id,
                                is_mainnet=True).public_view()


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (migrated through 0009) and psql")
class SuspenseReleaseDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.api.store import SqlStore

        cls.db = ApiRoleRunner(DB_URL)                         # app_api: everything under test
        cls.exe = ApiRoleRunner(DB_URL, role="app_executor")   # what deposits-scan writes
        cls.store = SqlStore()
        svc = _Svc(cls.store)
        svc.audit = Audit(cls.store)
        svc.notifier = Notifier()
        svc.typed_data = TypedData()
        svc.settings = SimpleNamespace(treasury_address=TREASURY, hl_api_url="https://api.hyperliquid.xyz")
        svc.now = lambda: datetime.now(timezone.utc)
        cls.svc = svc
        tag = uuid.uuid4().hex[:8]
        cls.tag = tag

        def user(name: str, role: str = "user") -> dict:
            u = cls.store.create_user(cls.db, firebase_uid=f"fb{name}{tag}", email=f"{name}{tag}@x.io", display_name=name,
                                      referral_code=f"{name.upper()}{tag}", referred_by=None, mfa_enrolled=True)
            # the admin ROLE is enforced by the route (admin_step_up), not by app/api/suspense.py; granting it here
            # is not needed (and admin grants may be restricted to a break-glass function)
            return u

        cls.a = user("adma", "admin")
        cls.b = user("admb", "admin")
        cls.u = user("usr")
        cls.v = user("usv")

    # ------------------------------------------------------------------------------------------------ helpers
    def ctx(self, u: dict) -> SimpleNamespace:
        return SimpleNamespace(user_id=str(u["id"]), actor=f"admin:{u['id']}", ip_hash="iphash")

    def hold(self, seed: str, sender: str, amount: int, *, record: bool = True) -> str:
        """Book a held transfer exactly like app.jobs_data.deposits._book_held (+ _record_held)."""
        from app.ledger import service as ledger

        h = _hash(self.tag + seed)
        tx = ledger.post_transaction(self.exe, f"usdc_hl:{h}", "deposit_held", f"USDC held for review {h[:10]}…",
                                     [("treasury:hl_usdc", amount), (SUSPENSE, -amount)], "system:deposits_scan")
        if record:
            self.exe.fetchall("""INSERT INTO usdc_held_deposits (tx_hash, sender_address, amount_micro, reason,
                                                                 transfer_time, held_tx_id)
                                 VALUES (:h, :s, :a, 'sender is not a verified wallet', now(), CAST(:t AS uuid))
                                 RETURNING tx_hash""", {"h": h, "s": sender, "a": amount, "t": str(tx.id)})
        return h

    def balance(self, code: str) -> int:
        return self.svc.ledger.balance(self.db, code)

    def audits(self, target: str) -> list[str]:
        return [r["action"] for r in self.db.fetchall(
            "SELECT action FROM audit_log WHERE target = :t ORDER BY seq", {"t": target})]

    # ------------------------------------------------------------------------------------------------ tests
    def test_attribute_to_verified_sender_maker_checker(self) -> None:
        from app.api import suspense

        s, svc, db = self.store, self.svc, self.db
        w = _addr(self.tag + "w1")
        s.upsert_verified_wallet(db, str(self.u["id"]), w, svc.now())
        h = self.hold("a1", w, 40_000_000)
        held = s.get_held_deposit(db, h)
        self.assertEqual((held["amount_micro"], held["sender_address"], held["release_id"]), (40_000_000, w, None))
        self.assertIn(h, [r["tx_hash"] for r in s.list_held_deposits(db, open_only=True, limit=500, cursor=None)])
        suspense_before = s.suspense_balance(db)

        # a user whose verified wallets do not include the sender cannot be credited
        with self.assertRaises(Forbidden):
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="attribute", user_id=str(self.v["id"]),
                             sender_address=None, evidence="support ticket 123")
        # nor the proposing admin themself
        with self.assertRaises(Forbidden):
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="attribute", user_id=str(self.a["id"]),
                             sender_address=None, evidence="support ticket 123")
        # a sender_address that contradicts the recorded one is refused
        with self.assertRaises(ValidationFailed):
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="attribute", user_id=str(self.u["id"]),
                             sender_address=_addr("other"), evidence="support ticket 123")
        rel = suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="attribute", user_id=str(self.u["id"]),
                               sender_address=None, evidence="support ticket 123: wallet verified 30 Sep")
        self.assertEqual((rel["status"], rel["sender_source"], str(rel["user_id"])), ("proposed", "scan", str(self.u["id"])))
        with self.assertRaises(Conflict):                                   # one live release per transfer
            suspense.propose(db, svc, self.ctx(self.b), tx_hash=h, action="refund", user_id=None, sender_address=None,
                             evidence="duplicate proposal")
        with self.assertRaises(Forbidden):                                  # no self-approval
            suspense.approve(db, svc, self.ctx(self.a), release_id=str(rel["id"]), reason="looks right to me")
        self.assertEqual(self.balance(f"user:{self.u['id']}:fee_balance"), 0)

        out = suspense.approve(db, svc, self.ctx(self.b), release_id=str(rel["id"]), reason="evidence checked")
        self.assertEqual((out["status"], str(out["checker_admin"])), ("approved", str(self.b["id"])))
        self.assertEqual(-self.balance(f"user:{self.u['id']}:fee_balance"), 40_000_000)
        self.assertEqual(s.suspense_balance(db), suspense_before - 40_000_000)
        tx = db.fetchall("""SELECT t.kind, a.code, e.amount_micro FROM ledger_transactions t
                              JOIN ledger_entries e ON e.tx_id = t.id JOIN ledger_accounts a ON a.id = e.account_id
                             WHERE t.idempotency_key = :k ORDER BY e.amount_micro""", {"k": f"suspense_release:{h}"})
        self.assertEqual([(r["kind"], r["code"], r["amount_micro"]) for r in tx],
                         [("suspense_release", f"user:{self.u['id']}:fee_balance", -40_000_000),
                          ("suspense_release", SUSPENSE, 40_000_000)])
        dep = db.fetchall("""SELECT status::text AS s, method::text AS m, withdrawable, amount_micro FROM deposits
                              WHERE external_ref = :h""", {"h": h})
        self.assertEqual(dep, [{"s": "credited", "m": "usdc_hl", "withdrawable": True, "amount_micro": 40_000_000}])
        self.assertIn({"user_id": str(self.u["id"]), "severity": "info", "kind": "topup_credited",
                       "payload": {"amount_micro": 40_000_000, "method": "USDC (held deposit released)"}},
                      svc.notifier.sent)
        self.assertNotIn(h, [r["tx_hash"] for r in s.list_held_deposits(db, open_only=True, limit=500, cursor=None)])
        self.assertEqual(s.get_held_deposit(db, h)["release_status"], "approved")
        with self.assertRaises(NotFound):                                   # decided: nothing to approve again
            suspense.approve(db, svc, self.ctx(self.b), release_id=str(rel["id"]), reason="evidence checked")
        # the ledger key is spent: a second posting under it (any content) is impossible
        from app.api import ledger_ops
        again = ledger_ops.release_suspense_to_user(db, svc, tx_hash=h, user_id=str(self.u["id"]), amount=40_000_000,
                                                    actor="test")
        self.assertEqual(again, str(out["release_tx_id"]))
        self.assertEqual(-self.balance(f"user:{self.u['id']}:fee_balance"), 40_000_000)
        with self.assertRaises(Exception):
            ledger_ops.release_suspense_to_refund(db, svc, tx_hash=h, amount=40_000_000, actor="test")
        self.assertEqual(self.audits(f"held_deposit:{h}"), ["suspense.propose.attribute", "suspense.approve.attribute"])
        # DB guard: a decided row cannot be re-opened or altered, even by a buggy caller
        from app.db.engine import DbError
        with self.assertRaises(DbError) as cm:
            db.fetchall("UPDATE suspense_releases SET status = 'proposed', decided_at = NULL WHERE id = CAST(:i AS uuid) "
                        "RETURNING id", {"i": str(rel["id"])})
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        with self.assertRaises(DbError) as cm:
            db.fetchall("UPDATE suspense_releases SET amount_micro = 1 WHERE id = CAST(:i AS uuid) RETURNING id",
                        {"i": str(rel["id"])})
        self.assertEqual(cm.exception.sqlstate, "AJ422")
        with self.assertRaises(DbError) as cm:
            db.fetchall("DELETE FROM suspense_releases WHERE id = CAST(:i AS uuid) RETURNING id", {"i": str(rel["id"])})
        self.assertEqual(cm.exception.sqlstate, "42501")

    def test_refund_reject_repropose_approve_send(self) -> None:
        from app.api import suspense

        s, svc, db = self.store, self.svc, self.db
        stranger = _addr(self.tag + "stranger")
        h = self.hold("r1", stranger, 25_000_000)
        treasury_before = self.balance("treasury:hl_usdc")
        with self.assertRaises(ValidationFailed):                           # refunds never name a user
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="refund", user_id=str(self.u["id"]),
                             sender_address=None, evidence="unknown sender, refund")
        r1 = suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="refund", user_id=None, sender_address=None,
                              evidence="unknown sender, refund per policy")
        with self.assertRaises(Forbidden):                                  # the maker cannot reject either
            suspense.reject(db, svc, self.ctx(self.a), release_id=str(r1["id"]), reason="changed my mind")
        rj = suspense.reject(db, svc, self.ctx(self.b), release_id=str(r1["id"]), reason="ask the sender first")
        self.assertEqual(rj["status"], "rejected")
        self.assertIsNone(s.get_held_deposit(db, h)["release_id"])          # open again
        r2 = suspense.propose(db, svc, self.ctx(self.b), tx_hash=h, action="refund", user_id=None, sender_address=None,
                              evidence="sender asked for a refund, ticket 77")
        with self.assertRaises(Conflict):                                   # not approved yet: nothing to sign
            suspense.refund_typed_data(db, svc, self.ctx(self.a), release_id=str(r2["id"]), signature_chain_id="0xa4b1")
        ap = suspense.approve(db, svc, self.ctx(self.a), release_id=str(r2["id"]), reason="ticket 77 verified")
        self.assertEqual(ap["status"], "approved")
        self.assertEqual(-self.balance(REFUNDS) >= 25_000_000, True)
        rel, payload = suspense.refund_typed_data(db, svc, self.ctx(self.a), release_id=str(r2["id"]),
                                                  signature_chain_id="0xa4b1")
        msg = payload["typed_data"]["message"]
        self.assertEqual((msg["destination"], msg["amount"], msg["hyperliquidChain"]), (stranger, "25", "Mainnet"))
        self.assertEqual(payload["action"]["type"], "usdSend")
        refund_hash = _hash(self.tag + "refund-sent")
        suspense.check_refund_sendable(db, svc, release_id=str(r2["id"]), refund_tx_hash=refund_hash)
        sent = suspense.record_refund_sent(db, svc, self.ctx(self.b), release_id=str(r2["id"]),
                                           refund_tx_hash=refund_hash, time_ms=1_790_000_000_000)
        self.assertEqual((sent["status"], sent["refund_tx_hash"]), ("sent", refund_hash))
        self.assertEqual(self.balance("treasury:hl_usdc"), treasury_before - 25_000_000)
        self.assertTrue(s.tx_hash_used(db, refund_hash))                    # cannot be reused for a payout either
        with self.assertRaises(Conflict):
            suspense.record_refund_sent(db, svc, self.ctx(self.b), release_id=str(r2["id"]), refund_tx_hash=refund_hash)
        keys = [r["idempotency_key"] for r in db.fetchall(
            "SELECT idempotency_key FROM ledger_transactions WHERE idempotency_key LIKE :p ORDER BY seq",
            {"p": f"%{h}%"})]
        self.assertEqual(keys, [f"usdc_hl:{h}", f"suspense_release:{h}", f"suspense_refund:{h}:sent"])
        self.assertEqual(self.audits(f"held_deposit:{h}"), [
            "suspense.propose.refund", "suspense.reject.refund", "suspense.propose.refund", "suspense.approve.refund",
            "suspense.refund.typed_data", "suspense.refund.sent"])
        self.assertEqual(self.db.fetchall("SELECT count(*) AS n FROM verify_chain()")[0]["n"], 0)

    def test_legacy_hold_without_recorded_sender_needs_onchain_verified_sender(self) -> None:
        from app.api import suspense

        svc, db = self.svc, self.db
        sender = _addr(self.tag + "legacy")
        h = self.hold("l1", sender, 12_000_000, record=False)
        self.assertIsNone(self.store.get_held_deposit(db, h)["sender_address"])
        with self.assertRaises(ValidationFailed):
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="refund", user_id=None, sender_address=None,
                             evidence="legacy hold")
        with self.assertRaises(ValidationFailed):                           # not verified on-chain by the route
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="refund", user_id=None, sender_address=sender,
                             evidence="legacy hold")
        rel = suspense.propose(db, svc, self.ctx(self.a), tx_hash=h, action="refund", user_id=None, sender_address=sender,
                               evidence="legacy hold", onchain_verified_sender=sender)
        self.assertEqual((rel["sender_address"], rel["sender_source"]), (sender, "onchain"))
        with self.assertRaises(NotFound):
            suspense.propose(db, svc, self.ctx(self.a), tx_hash=_hash("nope" + self.tag), action="refund", user_id=None,
                             sender_address=None, evidence="no such hold")

    def test_db_checks_four_eyes_and_beneficiary(self) -> None:
        from app.db.engine import DbError

        h = self.hold("c1", _addr(self.tag + "c1"), 1_000_000)
        held = self.store.get_held_deposit(self.db, h)
        with self.assertRaises(DbError) as cm:                               # maker = beneficiary
            self.db.fetchall("""INSERT INTO suspense_releases (tx_hash, held_tx_id, amount_micro, action, user_id,
                                    sender_address, sender_source, evidence, maker_admin)
                                VALUES (:h, CAST(:t AS uuid), 1000000, 'attribute', CAST(:m AS uuid), :s, 'scan',
                                        'evidence', CAST(:m AS uuid)) RETURNING id""",
                             {"h": h, "t": str(held["held_tx_id"]), "m": str(self.a["id"]), "s": _addr(self.tag + "c1")})
        self.assertEqual(cm.exception.sqlstate, "23514")
        row = self.store.insert_suspense_release(self.db, tx_hash=h, held_tx_id=str(held["held_tx_id"]),
                                                 amount_micro=1_000_000, action="refund", user_id=None,
                                                 sender_address=_addr(self.tag + "c1"), sender_source="scan",
                                                 evidence="evidence", maker=str(self.a["id"]))
        self.assertIsNone(self.store.approve_suspense_release(self.db, str(row["id"]), checker=str(self.a["id"]),
                                                              now=datetime.now(timezone.utc), reason="self approval",
                                                              release_tx_id=str(held["held_tx_id"])))
        with self.assertRaises(DbError) as cm:                               # even bypassing the store's guard
            self.db.fetchall("""UPDATE suspense_releases SET status = 'approved', checker_admin = maker_admin,
                                       decided_at = now(), release_tx_id = held_tx_id
                                 WHERE id = CAST(:i AS uuid) RETURNING id""", {"i": str(row["id"])})
        self.assertEqual(cm.exception.sqlstate, "23514")


if __name__ == "__main__":
    unittest.main()
