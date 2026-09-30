"""Stdlib-only tests for app/api input validation, SIWE parsing, cursor encoding, hashing, EIP-191 recovery and
the API ledger account/charge rules. No FastAPI/pydantic needed (runs with `python -m unittest`)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import ledger_ops  # noqa: E402
from app.api import validation as v  # noqa: E402
from app.api.ethsig import personal_message_hash, recover_address, recover_personal_sign  # noqa: E402
from app.errors import InsufficientBalance  # noqa: E402

UTC = timezone.utc


class AmountTests(unittest.TestCase):
    def test_usd_strings(self) -> None:
        self.assertEqual(v.usd_string_to_micro("10"), 10_000_000)
        self.assertEqual(v.usd_string_to_micro("12.34"), 12_340_000)
        self.assertEqual(v.usd_string_to_micro("0.000001"), 1)
        for bad in ("", "-1", "1e3", "1.0000001", "01", "1,000", " 1", "abc", "0", "0.0", "NaN", "Infinity"):
            with self.subTest(bad=bad), self.assertRaises(v.InputError):
                v.usd_string_to_micro(bad)
        with self.assertRaises(v.InputError):
            v.usd_string_to_micro("100000001")          # above the $100M sanity bound

    def test_micro_ints(self) -> None:
        self.assertEqual(v.check_micro(5), 5)
        self.assertEqual(v.check_micro(0, allow_zero=True), 0)
        for bad in (0, -1, True, 1.5, "5", v.MAX_AMOUNT_MICRO + 1):
            with self.subTest(bad=bad), self.assertRaises(v.InputError):
                v.check_micro(bad)  # type: ignore[arg-type]

    def test_formatting(self) -> None:
        self.assertEqual(v.micro_to_usd_string(10_500_000), "10.5")
        self.assertEqual(v.micro_to_usd_string(25_000_000), "25")
        self.assertEqual(v.micro_to_usd_string(1), "0.000001")
        self.assertEqual(v.tenths_bp_to_percent_string(100), "0.1%")
        self.assertEqual(v.tenths_bp_to_percent_string(10), "0.01%")


class AddressAndIdTests(unittest.TestCase):
    def test_address(self) -> None:
        self.assertEqual(v.normalize_address("0x" + "AB" * 20), "0x" + "ab" * 20)
        for bad in ("0x123", "ab" * 21, "0x" + "g" * 40, None, "0x" + "a" * 41):
            with self.subTest(bad=bad), self.assertRaises(v.InputError):
                v.normalize_address(bad)  # type: ignore[arg-type]

    def test_patterns(self) -> None:
        self.assertTrue(v.COIN_RE.match("xyz:SILVER") and v.COIN_RE.match("BTC") and v.COIN_RE.match("kPEPE"))
        self.assertFalse(v.COIN_RE.match("xyz:SILVER; DROP"))
        self.assertTrue(v.SLUG_RE.match("crest-silver") and not v.SLUG_RE.match("-bad") and not v.SLUG_RE.match("A"))
        self.assertTrue(v.IDEMPOTENCY_KEY_RE.match("3f1c2a9e-5b8d-4c1e-9a7b-0d2e4f6a8b1c"))
        self.assertFalse(v.IDEMPOTENCY_KEY_RE.match("short"))
        self.assertFalse(v.REQUEST_ID_RE.match("bad id with spaces"))


class CursorTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        ts = datetime(2026, 9, 30, 12, 0, 1, 123456, tzinfo=UTC)
        rid = "0b7c1d2e-3f40-4a5b-8c6d-7e8f90a1b2c3"
        self.assertEqual(v.decode_cursor(v.encode_cursor(ts, rid)), (ts, rid))
        self.assertIsNone(v.decode_cursor(None))

    def test_garbage_rejected(self) -> None:
        for bad in ("!!!", "eyJ0IjoxfQ", "x" * 300, v.encode_cursor(datetime(2026, 1, 1, tzinfo=UTC), "x")[:-3] + "AAA"):
            with self.subTest(bad=bad), self.assertRaises(v.InputError):
                v.decode_cursor(bad)


class HashingTests(unittest.TestCase):
    def test_peppered_and_normalised(self) -> None:
        pep = b"p" * 32
        a = v.hash_identifier("::ffff:1.2.3.4", pep)
        self.assertEqual(a, v.hash_identifier("1.2.3.4", pep))            # IPv4-mapped IPv6 normalised
        self.assertNotEqual(a, v.hash_identifier("1.2.3.4", b"q" * 32))   # pepper matters
        self.assertNotEqual(a, v.hash_identifier("1.2.3.4", pep, domain="ua"))
        self.assertIsNone(v.hash_identifier(None, pep))
        self.assertEqual(len(a or ""), 64)

    def test_matches_audit_module(self) -> None:
        try:
            from app.security.audit import hash_ip
        except Exception:  # pragma: no cover - module owned by another team
            self.skipTest("app.security.audit not importable")
        pep = b"k" * 32
        self.assertEqual(v.hash_identifier("10.0.0.1", pep), hash_ip("10.0.0.1", pep))


SIWE = """aijalon.trade wants you to sign in with your Ethereum account:
0x2c7536E3605D9C16a7a3D7b1898e529396a65c23

Prove you control this wallet for aijalon.trade.

URI: https://aijalon.trade
Version: 1
Chain ID: 42161
Nonce: AbCdEf0123456789XyZ
Issued At: 2026-09-30T12:00:00.000Z"""


class SiweTests(unittest.TestCase):
    def test_parse(self) -> None:
        m = v.parse_siwe(SIWE)
        self.assertEqual(m["domain"], "aijalon.trade")
        self.assertEqual(m["address"].lower(), "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23")
        self.assertEqual(m["Nonce"], "AbCdEf0123456789XyZ")
        self.assertEqual(v.parse_issued_at(m["Issued At"]), datetime(2026, 9, 30, 12, tzinfo=UTC))

    def test_rejects_malformed(self) -> None:
        cases = [SIWE.replace("\n", "\r\n"), SIWE.replace("Version: 1", "Version: 2"),
                 SIWE.replace("Nonce: AbCdEf0123456789XyZ", "Nonce: short!"),
                 SIWE + "\nNonce: again1234567", SIWE.replace("URI: https://aijalon.trade\n", ""),
                 "evil.example wants to sign" + SIWE[SIWE.index("\n"):], "x" * 3000]
        for bad in cases:
            with self.subTest(bad=bad[:40]), self.assertRaises(v.InputError):
                v.parse_siwe(bad)

    def test_message_is_signed_as_is(self) -> None:
        # the hash is over the exact text; any byte change changes the signer
        self.assertNotEqual(personal_message_hash(SIWE), personal_message_hash(SIWE + " "))


class Eip191Tests(unittest.TestCase):
    def test_web3_vector(self) -> None:
        sig = ("0xb91467e570a6466aa9e9876cbcd013baba02900b8979d43fe208a4a4f339f5fd6007e74cd82e037b800186422fc2da167c7"
               "47ef045e5d18a5f5d4300f8e1a0291c")
        self.assertEqual(recover_personal_sign("Some data", sig), "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23")
        self.assertNotEqual(recover_personal_sign("Some data!", sig), "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23")

    def test_rejects_bad_signatures(self) -> None:
        h = personal_message_hash("x")
        for bad in (b"\x00" * 65, b"\x01" * 64, b"\x01" * 32 + b"\xff" * 32 + b"\x1b", b"\x01" * 64 + b"\x05"):
            with self.subTest(bad=bad[-3:]), self.assertRaises(ValueError):
                recover_address(h, bad)


class _Ledger:
    """Tiny ledger double (sign rules as the real one) for ledger_ops unit tests."""

    def __init__(self) -> None:
        self.bal: dict[str, int] = {}
        self.txs: dict[str, list] = {}
        self.accounts: set[str] = set()

    def ensure_account(self, conn, code):
        ledger_ops.account_spec(code)
        self.accounts.add(code)

    def post(self, conn, *, idempotency_key, kind, memo, entries, created_by):
        if idempotency_key in self.txs:
            return idempotency_key
        for code, amt in entries:
            after = self.bal.get(code, 0) + amt
            if code.startswith(("user:", "creator:", "referrer:")) and after > 0 and amt > 0:
                raise InsufficientBalance("insufficient")
        for code, amt in entries:
            self.bal[code] = self.bal.get(code, 0) + amt
        self.txs[idempotency_key] = list(entries)
        return idempotency_key

    def balance(self, conn, code):
        return self.bal.get(code, 0)


def _svc() -> SimpleNamespace:
    from app.domain import fees
    return SimpleNamespace(ledger=_Ledger(), domain=SimpleNamespace(
        subscription_split=fees.subscription_split, post_sale_split=fees.post_sale_split, plan_price=fees.plan_price))


class LedgerOpsTests(unittest.TestCase):
    UID = "11111111-1111-4111-8111-111111111111"
    CREATOR = "22222222-2222-4222-8222-222222222222"

    def test_account_specs(self) -> None:
        self.assertEqual(ledger_ops.account_spec(f"user:{self.UID}:fee_balance"), ("liability", True, self.UID))
        self.assertEqual(ledger_ops.account_spec("platform:revenue:posts"), ("revenue", False, None))
        self.assertEqual(ledger_ops.account_spec("withdrawals:pending"), ("liability", False, None))
        with self.assertRaises(ValueError):
            ledger_ops.account_spec("mystery:account")

    def test_subscription_charge_split_and_idempotency(self) -> None:
        svc = _svc()
        svc.ledger.bal[ledger_ops.fee_balance(self.UID)] = -50_000_000
        svc.ledger.bal[ledger_ops.ACC_TREASURY] = 50_000_000          # the deposit's debit side
        st = {"slug": "x", "price_monthly_micro": 20_000_000, "in_house": False, "owner_user_id": self.CREATOR}
        charged, _ = ledger_ops.charge_subscription_start(None, svc, user_id=self.UID, subscription_id="s1", strategy=st,
                                                          actor="user:x")
        ledger_ops.charge_subscription_start(None, svc, user_id=self.UID, subscription_id="s1", strategy=st, actor="user:x")
        self.assertEqual(charged, 20_000_000)
        self.assertEqual(ledger_ops.spendable(None, svc, self.UID), 30_000_000)      # charged once
        self.assertEqual(svc.ledger.bal[ledger_ops.creator_payable(self.CREATOR)], -19_400_000)
        self.assertEqual(svc.ledger.bal[ledger_ops.ACC_SUBSCRIPTION_REVENUE], -600_000)
        self.assertEqual(sum(svc.ledger.bal.values()), 0)

    def test_free_strategy_charges_nothing(self) -> None:
        svc = _svc()
        st = {"slug": "silver", "price_monthly_micro": 0, "in_house": True, "owner_user_id": None}
        self.assertEqual(ledger_ops.charge_subscription_start(None, svc, user_id=self.UID, subscription_id="s2",
                                                              strategy=st, actor="user:x"), (0, None))
        self.assertEqual(svc.ledger.txs, {})

    def test_in_house_revenue_all_platform_and_posts(self) -> None:
        svc = _svc()
        svc.ledger.bal[ledger_ops.fee_balance(self.UID)] = -100_000_000
        ledger_ops.charge_subscription_start(None, svc, user_id=self.UID, subscription_id="s3", actor="user:x",
                                             strategy={"slug": "b", "price_monthly_micro": 5_000_000, "in_house": True})
        self.assertEqual(svc.ledger.bal[ledger_ops.ACC_SUBSCRIPTION_REVENUE], -5_000_000)
        price, _ = ledger_ops.charge_post(None, svc, user_id=self.UID, actor="user:x",
                                          post_row={"id": "p1", "price_micro": 5_000_000, "creator_id": self.CREATOR})
        self.assertEqual(price, 5_000_000)
        self.assertEqual(svc.ledger.bal[ledger_ops.creator_payable(self.CREATOR)], -4_000_000)   # price − $1
        self.assertEqual(svc.ledger.bal[ledger_ops.ACC_POSTS_REVENUE], -1_000_000)

    def test_require_balance_and_withdrawal_hold_release(self) -> None:
        svc = _svc()
        svc.ledger.bal[ledger_ops.fee_balance(self.UID)] = -30_000_000
        with self.assertRaises(InsufficientBalance):
            ledger_ops.require_balance(None, svc, self.UID, 30_000_001)
        ledger_ops.hold_withdrawal(None, svc, user_id=self.UID, withdrawal_id="w1", amount=30_000_000, actor="u")
        self.assertEqual(ledger_ops.spendable(None, svc, self.UID), 0)
        row = {"id": "w1", "amount_micro": 30_000_000}
        ledger_ops.release_hold(None, svc, kind="withdrawal", row=row, source_account=ledger_ops.fee_balance(self.UID),
                                actor="admin:x")
        self.assertEqual(ledger_ops.spendable(None, svc, self.UID), 30_000_000)
        self.assertEqual(svc.ledger.bal[ledger_ops.ACC_WITHDRAWALS_PENDING], 0)

    def test_unbalanced_refused(self) -> None:
        with self.assertRaises(AssertionError):
            ledger_ops.post(None, _svc(), key="k", kind="x", memo="", created_by="t", entries=[("a:b", 1)])


if __name__ == "__main__":
    unittest.main()
