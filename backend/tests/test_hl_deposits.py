"""app.hl.deposits on real (pseudonymized) ledger updates + hand-off to app.payments.usdc."""
from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.hl.deposits import detect_deposits  # noqa: E402
from app.hl.fake import load_fixture  # noqa: E402

TREASURY = "0x7714e80755d24782f18bae48a9e065127b905af4"   # receives a legacy internalTransfer + a spot USDC send
LEGACY_SENDER = "0xa8d2cde287577815d2c16e6b09b17910ffc26376"
SPOT_SENDER = "0xc71933a9e0c1065b08ffecf3394f564670dedcd9"


def ledger():
    return copy.deepcopy(load_fixture("userNonFundingLedgerUpdates_sample"))


class Detect(unittest.TestCase):
    def test_real_ledger(self) -> None:
        scan = detect_deposits(ledger(), treasury_address=TREASURY.upper().replace("0X", "0x"),
                               verified_wallets={LEGACY_SENDER.upper().replace("0X", "0x")})
        self.assertEqual(len(scan.deposits), 1)
        d = scan.deposits[0]
        self.assertEqual((d.kind, d.user_address, d.amount_micro, d.fee_micro, d.fee_uncertain),
                         ("internalTransfer", LEGACY_SENDER, 1_898_380_000, 0, False))
        self.assertEqual(d.time, 1754286027439)
        self.assertRegex(d.hash, r"^0x[0-9a-f]{64}$")
        self.assertEqual(d.idempotency_key, f"usdc_hl:{d.hash}")
        # spot→spot USDC send from an unverified wallet: held; 7-decimal amount floored to the micro
        self.assertEqual(len(scan.unverified), 1)
        u = scan.unverified[0]
        self.assertEqual((u.kind, u.user_address, u.amount_micro, u.source_dex, u.destination_dex),
                         ("send", SPOT_SENDER, 3_665_917_541, "spot", "spot"))
        self.assertEqual(u.nonce, 1765340189663)
        # the bridge deposit in the treasury's own history has no sender
        self.assertEqual([x["delta"]["type"] for x in scan.unattributable], ["deposit"])
        # self-transfers between the treasury's own balances are not deposits
        self.assertTrue(any("self-transfer" in why for _, why in scan.skipped))

    def test_non_usdc_and_outbound(self) -> None:
        scan = detect_deposits(ledger(), treasury_address="0xc9d42e1dc710c2dc891ef303c50af453345f0c63")
        self.assertEqual([(d.kind, d.amount_micro, d.destination_dex) for d in scan.deposits],
                         [("send", 100_000_000, "")])  # plain perp→perp usdSend
        self.assertTrue(any("USDE" in why for _, why in scan.skipped))

    def test_send_with_fee_is_credited_conservatively(self) -> None:
        scan = detect_deposits(ledger(), treasury_address="0x95b3443dc982fcc6b28f70fe201f8eee25d64578")
        d, = scan.deposits
        self.assertEqual((d.gross_micro, d.fee_micro, d.amount_micro, d.fee_uncertain),
                         (100_000_000, 1_000_000, 99_000_000, True))

    def test_hash_rules_and_since(self) -> None:
        base = {"time": 1_790_000_000_000, "delta": {"type": "send", "user": LEGACY_SENDER, "destination": TREASURY,
                                                      "sourceDex": "", "destinationDex": "", "token": "USDC",
                                                      "amount": "25.5", "fee": "0.0", "nonce": 1_790_000_000_000}}
        good = {**base, "hash": "0x" + "ab" * 32}
        zero = {**base, "hash": "0x" + "0" * 64}
        dup = {**good, "time": base["time"] + 5}
        scan = detect_deposits([good, zero, dup], treasury_address=TREASURY)
        self.assertEqual([d.amount_micro for d in scan.deposits], [25_500_000])
        reasons = [why for _, why in scan.skipped]
        self.assertTrue(any("hash" in r for r in reasons))
        self.assertTrue(any("duplicate" in r for r in reasons))
        self.assertEqual(detect_deposits([good], treasury_address=TREASURY, since_ms=base["time"] + 1).deposits, [])

    def test_bad_inputs(self) -> None:
        with self.assertRaises(ValidationFailed):
            detect_deposits([], treasury_address="treasury")
        scan = detect_deposits([{"nope": 1}, {"time": 1, "delta": {"type": "send", "user": LEGACY_SENDER,
                                                                   "destination": TREASURY, "amount": "abc",
                                                                   "token": "USDC"}, "hash": "0x" + "cd" * 32}],
                               treasury_address=TREASURY)
        self.assertEqual(len(scan.skipped), 2)


class PaymentsHandOff(unittest.TestCase):
    def test_credit_from_detection(self) -> None:
        from app.payments.usdc import coerce_detection, credit_from_detection

        scan = detect_deposits(ledger(), treasury_address=TREASURY, verified_wallets={LEGACY_SENDER})
        d = scan.deposits[0]
        det = coerce_detection(d)
        self.assertEqual((det.tx_hash, det.from_address, det.to_address, det.amount_micro, det.time_ms),
                         (d.hash, LEGACY_SENDER, TREASURY, 1_898_380_000, 1754286027439))
        out = credit_from_detection(d, treasury_address=TREASURY, user_for_address=lambda a: "user-1")
        self.assertIsNotNone(out.credit)
        self.assertEqual((out.credit.amount_micro, out.credit.idempotency_key), (1_898_380_000, f"usdc_hl:{d.hash}"))
        held = credit_from_detection(scan.unverified[0], treasury_address=TREASURY, user_for_address=lambda a: None)
        self.assertIsNone(held.credit)
        self.assertIsNotNone(held.held)


if __name__ == "__main__":
    unittest.main()
