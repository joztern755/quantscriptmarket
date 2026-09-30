"""USDC top-up typed data + crediting from on-chain detections."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.payments.usdc import build_topup_request, credit_from_detection, exchange_body, format_usd_amount  # noqa: E402

USER_ADDR = "0x" + "ab" * 20
TREASURY = "0x" + "cd" * 20
TX = "0x" + "12" * 32
T = 1_790_000_000_000


class TypedDataTests(unittest.TestCase):
    def test_structure(self):
        r = build_topup_request(user_master_address=USER_ADDR.upper().replace("0X", "0x"), amount_micro=25_500_000,
                                treasury_address=TREASURY, signature_chain_id="0xa4b1", time_ms=T)
        td = r.typed_data
        self.assertEqual(td["primaryType"], "HyperliquidTransaction:UsdSend")
        self.assertEqual(td["domain"], {"name": "HyperliquidSignTransaction", "version": "1", "chainId": 42161,
                                        "verifyingContract": "0x0000000000000000000000000000000000000000"})
        self.assertEqual([f["name"] for f in td["types"]["HyperliquidTransaction:UsdSend"]],
                         ["hyperliquidChain", "destination", "amount", "time"])
        self.assertEqual(td["message"], {"hyperliquidChain": "Mainnet", "destination": TREASURY, "amount": "25.5", "time": T})
        self.assertEqual(r.action, {"type": "usdSend", "signatureChainId": "0xa4b1", "hyperliquidChain": "Mainnet",
                                    "destination": TREASURY, "amount": "25.5", "time": T})
        self.assertEqual(r.nonce, T)
        self.assertEqual(r.source, USER_ADDR)

    def test_testnet(self):
        r = build_topup_request(user_master_address=USER_ADDR, amount_micro=10_000_000, treasury_address=TREASURY,
                                signature_chain_id="0x66eee", time_ms=T, is_mainnet=False)
        self.assertEqual(r.typed_data["message"]["hyperliquidChain"], "Testnet")
        self.assertEqual(r.action["amount"], "10")

    def test_validation(self):
        base = dict(user_master_address=USER_ADDR, amount_micro=10_000_000, treasury_address=TREASURY,
                    signature_chain_id="0xa4b1", time_ms=T)
        bad = [dict(amount_micro=9_999_999), dict(amount_micro=10_000_001), dict(treasury_address="0x123"),
               dict(treasury_address=USER_ADDR), dict(signature_chain_id="42161"), dict(signature_chain_id="0x0"),
               dict(time_ms=0), dict(user_master_address="nope")]
        for b in bad:
            with self.subTest(b=b):
                with self.assertRaises(ValidationFailed):
                    build_topup_request(**{**base, **b})

    def test_format(self):
        self.assertEqual(format_usd_amount(10_000_000), "10")
        self.assertEqual(format_usd_amount(1_234_560_000), "1234.56")

    def test_exchange_body(self):
        r = build_topup_request(user_master_address=USER_ADDR, amount_micro=10_000_000, treasury_address=TREASURY,
                                signature_chain_id="0xa4b1", time_ms=T)
        body = exchange_body(r.action, {"r": "0x1", "s": "0x2", "v": 27})
        self.assertEqual(body["nonce"], T)
        self.assertEqual(body["signature"], {"r": "0x1", "s": "0x2", "v": 27})


class DetectionTests(unittest.TestCase):
    def det(self, **kw):
        d = {"hash": TX, "user": USER_ADDR, "destination": TREASURY, "amount": "25.123456789", "time": T}
        d.update(kw)
        return d

    def test_credit(self):
        out = credit_from_detection(self.det(), treasury_address=TREASURY, user_for_address=lambda a: "u1" if a == USER_ADDR else None)
        c = out.credit
        self.assertEqual((c.user_id, c.amount_micro, c.external_ref, c.idempotency_key),
                         ("u1", 25_123_456, TX, f"usdc_hl:{TX}"))  # floor to micro
        self.assertEqual((c.debit_account, c.credit_account), ("treasury:hl_usdc", "user:u1:fee_balance"))
        self.assertTrue(c.withdrawable)

    def test_idempotent(self):
        a = credit_from_detection(self.det(), treasury_address=TREASURY, user_for_address=lambda a: "u1")
        b = credit_from_detection(self.det(), treasury_address=TREASURY, user_for_address=lambda a: "u1")
        self.assertEqual(a.credit, b.credit)

    def test_not_to_treasury(self):
        out = credit_from_detection(self.det(destination="0x" + "ee" * 20), treasury_address=TREASURY,
                                    user_for_address=lambda a: "u1")
        self.assertIsNone(out.credit)
        self.assertEqual(out.ignored, "not sent to treasury")

    def test_below_min_held(self):
        out = credit_from_detection(self.det(amount="9.99"), treasury_address=TREASURY, user_for_address=lambda a: "u1")
        self.assertIsNone(out.credit)
        self.assertIn("below minimum", out.held)
        self.assertTrue(out.alerts)

    def test_unknown_sender_held(self):
        out = credit_from_detection(self.det(), treasury_address=TREASURY, user_for_address=lambda a: None)
        self.assertIsNone(out.credit)
        self.assertEqual(out.held, "sender is not a verified user wallet")

    def test_float_amount_rejected(self):
        with self.assertRaises(ValidationFailed):
            credit_from_detection(self.det(amount=25.5), treasury_address=TREASURY, user_for_address=lambda a: "u1")

    def test_bad_hash(self):
        with self.assertRaises(ValidationFailed):
            credit_from_detection(self.det(hash="0x12"), treasury_address=TREASURY, user_for_address=lambda a: "u1")

    def test_scanner_resolved_user(self):
        out = credit_from_detection(self.det(user_id="u9"), treasury_address=TREASURY)
        self.assertEqual(out.credit.user_id, "u9")


if __name__ == "__main__":
    unittest.main()
