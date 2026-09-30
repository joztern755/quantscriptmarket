"""create_topup_intent: params, limits, idempotency, MYR rate lock; HTTP gateway request formation."""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from urllib.parse import urlencode

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import Conflict, ExternalServiceError, Forbidden, ValidationFailed  # noqa: E402
from app.payments.stripe_pay import (  # noqa: E402
    FxQuote,
    StripeHttpGateway,
    StripeTopupConfig,
    _form_encode,
    create_topup_intent,
    handle_event,
)

USER = "8c1f6a1e-0000-4000-8000-000000000001"
TOKEN = "3f2b7c1d9e8a4b6c"


class FakeGateway:
    def __init__(self):
        self.calls = []

    def create_payment_intent(self, params, key):
        self.calls.append((params, key))
        return {"id": "pi_abc", "client_secret": "pi_abc_secret_xyz", "amount": params["amount"],
                "currency": params["currency"], "metadata": params["metadata"], "status": "requires_payment_method"}

    def retrieve_payment_intent(self, pi_id):  # pragma: no cover
        raise AssertionError


class FakeFx:
    def __init__(self, rate="4.6630", quoted_at=1000.0, currency="myr"):
        self.q = FxQuote(currency, Decimal(rate), "test_feed", "fxq_1", quoted_at)

    def quote_usd_to(self, currency):
        return self.q


class User:
    def __init__(self, id, status="active"):
        self.id, self.status = id, status


class CreateIntentTests(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.cfg = StripeTopupConfig(env="test")

    def test_usd_params(self):
        ti = create_topup_intent(User(USER), 25_000_000, gateway=self.gw, idempotency=TOKEN, config=self.cfg)
        params, key = self.gw.calls[0]
        self.assertEqual(params["amount"], 2500)
        self.assertEqual(params["currency"], "usd")
        self.assertEqual(params["automatic_payment_methods"], {"enabled": True})
        self.assertEqual(params["metadata"]["user_id"], USER)
        self.assertEqual(params["metadata"]["purpose"], "fee_balance_topup")
        self.assertEqual(params["metadata"]["idempotency"], TOKEN)
        self.assertEqual(params["metadata"]["credit_micro"], "25000000")
        self.assertTrue(all(isinstance(v, str) for v in params["metadata"].values()))
        self.assertEqual(key, f"topup:{USER}:{TOKEN}")
        self.assertEqual((ti.payment_intent_id, ti.credit_micro, ti.amount_minor), ("pi_abc", 25_000_000, 2500))
        self.assertEqual(ti.public_view()["client_secret"], "pi_abc_secret_xyz")

    def test_limits(self):
        for amt in (9_990_000, 10_000_000_001):
            with self.assertRaises(ValidationFailed):
                create_topup_intent(USER, amt, gateway=self.gw, idempotency=TOKEN, config=self.cfg)
        create_topup_intent(USER, 10_000_000, gateway=self.gw, idempotency=TOKEN, config=self.cfg)
        create_topup_intent(USER, 10_000_000_000, gateway=self.gw, idempotency=TOKEN, config=self.cfg)

    def test_rejects_sub_cent_float_and_bad_inputs(self):
        with self.assertRaises(ValidationFailed):
            create_topup_intent(USER, 10_000_001, gateway=self.gw, idempotency=TOKEN, config=self.cfg)
        with self.assertRaises(ValidationFailed):
            create_topup_intent(USER, 10.0, gateway=self.gw, idempotency=TOKEN, config=self.cfg)  # type: ignore[arg-type]
        with self.assertRaises(ValidationFailed):
            create_topup_intent(USER, 10_000_000, "eur", gateway=self.gw, idempotency=TOKEN, config=self.cfg)
        with self.assertRaises(ValidationFailed):
            create_topup_intent(USER, 10_000_000, gateway=self.gw, idempotency="bad token!", config=self.cfg)
        with self.assertRaises(Forbidden):
            create_topup_intent(User(USER, "suspended"), 10_000_000, gateway=self.gw, idempotency=TOKEN, config=self.cfg)
        self.assertEqual(self.gw.calls, [])

    def test_fee_estimate_in_public_view(self):
        cfg = StripeTopupConfig(env="test", fee_estimate_bps=300, fee_estimate_fixed_micro=250_000)
        v = create_topup_intent(USER, 100_000_000, gateway=self.gw, idempotency=TOKEN, config=cfg).public_view()
        self.assertTrue(v["fee_passthrough"])
        self.assertEqual((v["estimated_fee_micro"], v["estimated_credit_micro"]), (3_250_000, 96_750_000))
        v = create_topup_intent(USER, 100_000_000, gateway=self.gw, idempotency=TOKEN, config=self.cfg).public_view()
        self.assertNotIn("estimated_fee_micro", v)
        self.assertIn("fee_note", v)

    def test_myr_disabled_by_default(self):
        with self.assertRaises(ValidationFailed):
            create_topup_intent(USER, 50_000_000, "myr", gateway=self.gw, idempotency=TOKEN, config=self.cfg,
                                fx_provider=FakeFx())

    def test_myr_rate_lock_and_roundtrip(self):
        cfg = StripeTopupConfig(env="test", myr_enabled=True, myr_fx_spread_bps=150)
        ti = create_topup_intent(USER, 50_000_000, "myr", gateway=self.gw, idempotency=TOKEN, config=cfg,
                                 fx_provider=FakeFx("4.6630", 1000.0), clock=lambda: 1010.0)
        # 50 × 4.6630 × 1.015 = 236.64725 -> ceil to sen = 236.65
        self.assertEqual(ti.amount_minor, 23665)
        params, _ = self.gw.calls[0]
        md = params["metadata"]
        self.assertEqual((md["amount_minor"], md["fx_mid_rate"], md["fx_spread_bps"], md["fx_source"]),
                         ("23665", "4.6630", "150", "test_feed"))
        # the webhook credits exactly the locked USD amount
        pi = {"id": "pi_abc", "object": "payment_intent", "amount": 23665, "amount_received": 23665,
              "currency": "myr", "status": "succeeded", "metadata": md}
        fetched = dict(pi, latest_charge={"balance_transaction": {"amount": 23665, "fee": 0, "currency": "myr"}})

        class G:
            def retrieve_payment_intent(self, pi_id, expand=()):
                return fetched

        out = handle_event({"id": "evt", "object": "event", "type": "payment_intent.succeeded", "livemode": False,
                            "data": {"object": pi}}, config=cfg, gateway=G())
        self.assertEqual(out.credits[0].amount_micro, 50_000_000)

    def test_myr_stale_or_insane_quote(self):
        cfg = StripeTopupConfig(env="test", myr_enabled=True)
        with self.assertRaises(ExternalServiceError):
            create_topup_intent(USER, 50_000_000, "myr", gateway=self.gw, idempotency=TOKEN, config=cfg,
                                fx_provider=FakeFx(quoted_at=1000.0), clock=lambda: 1100.0)
        with self.assertRaises(ExternalServiceError):
            create_topup_intent(USER, 50_000_000, "myr", gateway=self.gw, idempotency=TOKEN, config=cfg,
                                fx_provider=FakeFx(rate="46.63", quoted_at=1000.0), clock=lambda: 1000.0)
        with self.assertRaises(ExternalServiceError):
            create_topup_intent(USER, 50_000_000, "myr", gateway=self.gw, idempotency=TOKEN, config=cfg,
                                fx_provider=FakeFx(currency="sgd", quoted_at=1000.0), clock=lambda: 1000.0)

    def test_gateway_mismatch_detected(self):
        class Bad(FakeGateway):
            def create_payment_intent(self, params, key):
                r = super().create_payment_intent(params, key)
                r["amount"] = 1
                return r

        with self.assertRaises(Conflict):
            create_topup_intent(USER, 10_000_000, gateway=Bad(), idempotency=TOKEN, config=self.cfg)


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, data=None, headers=None, timeout=None, json=None):
        self.calls.append(("POST", url, data, headers, timeout))
        return self.resp

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers, timeout))
        return self.resp


class HttpGatewayTests(unittest.TestCase):
    def test_form_encoding(self):
        enc = urlencode(_form_encode({"amount": 1000, "automatic_payment_methods": {"enabled": True},
                                      "metadata": {"user_id": "u1"}, "skip": None, "list": ["a", "b"]}))
        self.assertEqual(enc, "amount=1000&automatic_payment_methods%5Benabled%5D=true&metadata%5Buser_id%5D=u1"
                              "&list%5B0%5D=a&list%5B1%5D=b")

    def test_create_request_formation(self):
        s = FakeSession(FakeResp(200, {"id": "pi_1", "client_secret": "x", "amount": 1000, "currency": "usd"}))
        gw = StripeHttpGateway("sk_test_abcdefgh12345678", session=s, api_version="2025-01-01")
        gw.create_payment_intent({"amount": 1000, "currency": "usd", "automatic_payment_methods": {"enabled": True}}, "idem-1")
        method, url, data, headers, timeout = s.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.stripe.com/v1/payment_intents"))
        self.assertIn(("automatic_payment_methods[enabled]", "true"), data)
        self.assertEqual(headers["Authorization"], "Bearer sk_test_abcdefgh12345678")
        self.assertEqual(headers["Idempotency-Key"], "idem-1")
        self.assertEqual(headers["Stripe-Version"], "2025-01-01")
        self.assertTrue(timeout)

    def test_error_mapping(self):
        gw = StripeHttpGateway("sk_test_x", session=FakeSession(FakeResp(400, {"error": {"type": "idempotency_error"}})))
        with self.assertRaises(Conflict):
            gw.create_payment_intent({"amount": 1}, "k")
        gw = StripeHttpGateway("sk_test_x", session=FakeSession(FakeResp(503, {})))
        with self.assertRaises(ExternalServiceError):
            gw.create_payment_intent({"amount": 1}, "k")
        gw = StripeHttpGateway("sk_test_x", session=FakeSession(FakeResp(402, {"error": {"type": "card_error"}})))
        with self.assertRaises(ValidationFailed):
            gw.create_payment_intent({"amount": 1}, "k")

    def test_retrieve_validates_id(self):
        s = FakeSession(FakeResp(200, {"id": "pi_1"}))
        gw = StripeHttpGateway("sk_test_x", session=s)
        with self.assertRaises(ValidationFailed):
            gw.retrieve_payment_intent("pi_1/../customers")
        self.assertEqual(gw.retrieve_payment_intent("pi_1", expand=("latest_charge.balance_transaction",))["id"], "pi_1")
        self.assertEqual(s.calls[0][1], "https://api.stripe.com/v1/payment_intents/pi_1")
        self.assertEqual(s.calls[0][2], [("expand[]", "latest_charge.balance_transaction")])


if __name__ == "__main__":
    unittest.main()
