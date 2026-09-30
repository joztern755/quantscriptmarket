"""Stripe webhook signature verification (our implementation; stripe lib not used)."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.payments.stripe_pay import WebhookVerificationError, compute_signature, verify_webhook  # noqa: E402

SECRET = "whsec_test_secret_value_123"
NOW = 1_790_000_000
EVENT = {"id": "evt_1", "object": "event", "type": "payment_intent.succeeded", "livemode": False,
         "data": {"object": {"id": "pi_1", "object": "payment_intent"}}}
PAYLOAD = json.dumps(EVENT, separators=(",", ":")).encode()


def header(payload: bytes = PAYLOAD, t: int = NOW, secrets=(SECRET,), extra: str = "") -> str:
    parts = [f"t={t}"] + [f"v1={compute_signature(payload, t, s)}" for s in secrets]
    if extra:
        parts.append(extra)
    return ",".join(parts)


def verify(payload=PAYLOAD, sig=None, secret=SECRET, now=NOW, tolerance=300):
    return verify_webhook(payload, sig if sig is not None else header(payload), secret, tolerance,
                          now=now, use_stripe_lib=False)


class SignatureTests(unittest.TestCase):
    def test_valid(self):
        ev = verify()
        self.assertEqual(ev["id"], "evt_1")

    def test_known_vector(self):
        # HMAC-SHA256(key, "t.payload") hex — independently computed with hashlib/hmac.
        import hashlib
        import hmac
        expected = hmac.new(b"k", b"123.{}", hashlib.sha256).hexdigest()
        self.assertEqual(compute_signature(b"{}", 123, "k"), expected)

    def test_invalid_signature(self):
        bad = f"t={NOW},v1={'0' * 64}"
        with self.assertRaises(WebhookVerificationError):
            verify(sig=bad)

    def test_wrong_secret(self):
        with self.assertRaises(WebhookVerificationError):
            verify(secret="whsec_other_secret_999")

    def test_tampered_payload(self):
        sig = header(PAYLOAD)
        tampered = PAYLOAD.replace(b"pi_1", b"pi_2")
        with self.assertRaises(WebhookVerificationError):
            verify(payload=tampered, sig=sig)

    def test_old_timestamp_rejected(self):
        sig = header(t=NOW - 301)
        with self.assertRaises(WebhookVerificationError):
            verify(sig=sig)

    def test_replay_after_window_rejected_but_ok_inside(self):
        sig = header(t=NOW)
        self.assertEqual(verify(sig=sig, now=NOW + 299)["id"], "evt_1")  # replay inside window: idempotency handles it
        with self.assertRaises(WebhookVerificationError):
            verify(sig=sig, now=NOW + 301)

    def test_future_timestamp_rejected(self):
        with self.assertRaises(WebhookVerificationError):
            verify(sig=header(t=NOW + 1000))

    def test_multiple_v1_one_valid(self):
        sig = f"t={NOW},v1={'a' * 64},v1={compute_signature(PAYLOAD, NOW, SECRET)},v0={'b' * 64}"
        self.assertEqual(verify(sig=sig)["id"], "evt_1")

    def test_multiple_v1_none_valid(self):
        sig = f"t={NOW},v1={'a' * 64},v1={'c' * 64}"
        with self.assertRaises(WebhookVerificationError):
            verify(sig=sig)

    def test_v0_only_rejected(self):
        sig = f"t={NOW},v0={compute_signature(PAYLOAD, NOW, SECRET)}"
        with self.assertRaises(WebhookVerificationError):
            verify(sig=sig)

    def test_secret_rotation_list(self):
        sig = header(secrets=("whsec_new_secret_abcdef",))
        ev = verify_webhook(PAYLOAD, sig, ["whsec_old_secret_abcdef", "whsec_new_secret_abcdef"], now=NOW,
                            use_stripe_lib=False)
        self.assertEqual(ev["type"], "payment_intent.succeeded")

    def test_empty_secret_fails_closed(self):
        with self.assertRaises(WebhookVerificationError):
            verify(secret="")
        with self.assertRaises(WebhookVerificationError):
            verify(secret=[])

    def test_malformed_headers(self):
        for sig in ("", "garbage", f"v1={compute_signature(PAYLOAD, NOW, SECRET)}", "t=abc,v1=" + "0" * 64,
                    f"t={NOW},t={NOW},v1=" + compute_signature(PAYLOAD, NOW, SECRET), "t=1," + "x" * 5000):
            with self.subTest(sig=sig[:40]):
                with self.assertRaises(WebhookVerificationError):
                    verify(sig=sig)

    def test_uppercase_hex_signature_accepted(self):
        sig = f"t={NOW},v1={compute_signature(PAYLOAD, NOW, SECRET).upper()}"
        self.assertEqual(verify(sig=sig)["id"], "evt_1")

    def test_str_payload_refused(self):
        with self.assertRaises(TypeError):
            verify_webhook(PAYLOAD.decode(), header(), SECRET, now=NOW, use_stripe_lib=False)

    def test_non_event_payload_rejected(self):
        body = b'{"hello":"world"}'
        with self.assertRaises(WebhookVerificationError):
            verify(payload=body, sig=header(body))
        body = b"not json"
        with self.assertRaises(WebhookVerificationError):
            verify(payload=body, sig=header(body))

    def test_stripe_lib_requested_but_missing(self):
        try:
            import stripe  # noqa: F401
            self.skipTest("stripe installed")
        except ImportError:
            pass
        with self.assertRaises(WebhookVerificationError):
            verify_webhook(PAYLOAD, header(), SECRET, now=NOW, use_stripe_lib=True)


if __name__ == "__main__":
    unittest.main()
