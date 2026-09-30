"""make_fee_lookup (actual Stripe fee from the charge's balance_transaction) and the Stripe webhook path end to end
with fakes: signed raw body -> verify_webhook -> handle_event(fee_lookup=make_fee_lookup(gateway)) -> credit of
gross - actual fee, spend-only (withdrawable False). The HTTP variant (real StripeAdapter behind the FastAPI app,
in-memory fakes for DB/ledger) runs only where FastAPI + httpx are installed (CI)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ExternalServiceError, ValidationFailed  # noqa: E402
from app.payments.stripe_pay import (  # noqa: E402
    FEE_EXPAND,
    StripeFeeNotReady,
    StripeTopupConfig,
    compute_signature,
    handle_event,
    make_fee_lookup,
    verify_webhook,
)

CFG = StripeTopupConfig(env="test")
USER = "8c1f6a1e-0000-4000-8000-000000000001"
SECRET = "whsec_test"
USD = 1_000_000


def pi(pi_id="pi_123", amount=5000, received=None, currency="usd", meta=None, user=USER):
    m = meta if meta is not None else {"user_id": user, "purpose": "fee_balance_topup", "idempotency": "tok_12345678",
                                       "env": "test", "credit_micro": str(amount * 10_000)}
    return {"id": pi_id, "object": "payment_intent", "amount": amount,
            "amount_received": amount if received is None else received, "currency": currency,
            "status": "succeeded", "metadata": m, "latest_charge": "ch_1"}   # raw webhook object: charge is an id


def expanded(p, bt_amount=None, bt_fee=0, bt=True):
    p = dict(p)
    p["latest_charge"] = {"id": "ch_1", "object": "charge",
                          "balance_transaction": ({"id": "txn_1", "amount": bt_amount or p["amount_received"] * 4,
                                                   "currency": "myr", "fee": bt_fee} if bt else None)}
    return p


class FakeGateway:
    def __init__(self, pis):
        self.pis = pis
        self.calls = []

    def retrieve_payment_intent(self, pi_id, expand=()):
        self.calls.append((pi_id, tuple(expand)))
        return self.pis[pi_id]

    def create_payment_intent(self, params, key):  # pragma: no cover
        raise AssertionError("not used")


def gw(p, **kw):
    return FakeGateway({p["id"]: expanded(p, **kw)})


class MakeFeeLookupTests(unittest.TestCase):
    def test_reads_expanded_pi_without_gateway_call(self):
        # $50 gross; bt 20000 sen with 1000 sen fee -> 5% -> $2.50
        g = FakeGateway({})
        fee = make_fee_lookup(g)(expanded(pi(), bt_amount=20_000, bt_fee=1_000))
        self.assertEqual(fee, 2_500_000)
        self.assertEqual(g.calls, [])

    def test_refetches_raw_pi_with_fee_expand(self):
        p = pi()
        g = gw(p, bt_amount=20_000, bt_fee=727)
        fee = make_fee_lookup(g)(p)
        self.assertEqual(fee, 50 * USD * 727 // 20_000)             # floored share
        self.assertEqual(g.calls, [("pi_123", FEE_EXPAND)])

    def test_no_gateway_and_not_expanded_is_retryable(self):
        with self.assertRaises(StripeFeeNotReady):
            make_fee_lookup(None)(pi())

    def test_balance_transaction_missing_after_refetch_is_retryable(self):
        p = pi()
        with self.assertRaises(StripeFeeNotReady):
            make_fee_lookup(gw(p, bt=False))(p)
        self.assertTrue(issubclass(StripeFeeNotReady, ExternalServiceError))   # -> 5xx -> Stripe redelivers

    def test_refetch_id_mismatch_rejected(self):
        p = pi()
        g = FakeGateway({"pi_123": expanded(pi(pi_id="pi_other"))})
        with self.assertRaises(ValidationFailed):
            make_fee_lookup(g)(p)

    def test_fee_not_less_than_amount_rejected(self):
        with self.assertRaises(ValidationFailed):
            make_fee_lookup(None)(expanded(pi(), bt_amount=100, bt_fee=100))

    def test_partial_receipt_uses_received_gross(self):
        p = expanded(pi(amount=5000, received=4000), bt_amount=16_000, bt_fee=800)   # 5% of $40
        self.assertEqual(make_fee_lookup(None)(p), 2 * USD)

    def test_myr_gross_is_locked_credit(self):
        meta = {"user_id": USER, "purpose": "fee_balance_topup", "idempotency": "tok_12345678", "env": "test",
                "credit_micro": "50000000", "amount_minor": "23665", "fx_rate_applied": "4.733"}
        p = expanded(pi(amount=23_665, currency="myr", meta=meta), bt_amount=23_665, bt_fee=1_183)
        self.assertEqual(make_fee_lookup(None)(p), 50 * USD * 1_183 // 23_665)

    def test_not_a_topup_rejected(self):
        p = expanded(pi(meta={"purpose": "other"}), bt_fee=10)
        with self.assertRaises(ValidationFailed):
            make_fee_lookup(None)(p)

    def test_matches_built_in_fee_path(self):
        p = pi()
        a = handle_event(_event(p), config=CFG, gateway=gw(p, bt_amount=20_000, bt_fee=731))
        b = handle_event(_event(p), config=CFG, gateway=gw(p, bt_amount=20_000, bt_fee=731),
                         fee_lookup=make_fee_lookup(gw(p, bt_amount=20_000, bt_fee=731)))
        self.assertEqual(a.credits[0].amount_micro, b.credits[0].amount_micro)
        self.assertEqual(a.credits[0].meta["fee_micro"], b.credits[0].meta["fee_micro"])


def _event(p, evt_id="evt_1"):
    return {"id": evt_id, "object": "event", "type": "payment_intent.succeeded", "livemode": False,
            "data": {"object": p}}


def _signed(event, secret=SECRET, t=None):
    raw = json.dumps(event, separators=(",", ":")).encode()
    t = int(time.time()) if t is None else t
    return raw, f"t={t},v1={compute_signature(raw, t, secret)}"


class WebhookPathTests(unittest.TestCase):
    """What StripeAdapter does for POST /v1/webhooks/stripe, minus FastAPI: verify on the raw body, then
    handle_event with the gateway AND make_fee_lookup(gateway)."""

    def run_path(self, p, g):
        raw, sig = _signed(_event(p))
        ev = verify_webhook(raw, sig, [SECRET], use_stripe_lib=False)
        return handle_event(ev, config=CFG, gateway=g, fee_lookup=make_fee_lookup(g))

    def test_credit_is_gross_minus_actual_fee_and_spend_only(self):
        p = pi(amount=2500)                                          # $25.00 card top-up
        g = gw(p, bt_amount=10_000, bt_fee=345)                      # 3.45% in MYR settlement
        out = self.run_path(p, g)
        self.assertIsNone(out.manual_review)
        c = out.credits[0]
        fee = 25 * USD * 345 // 10_000
        self.assertEqual(c.amount_micro, 25 * USD - fee)
        self.assertEqual((c.meta["gross_micro"], c.meta["fee_micro"]), (25 * USD, fee))
        self.assertFalse(c.withdrawable)
        self.assertEqual(c.idempotency_key, "stripe:pi_123")
        self.assertEqual(g.calls[0], ("pi_123", FEE_EXPAND))

    def test_fee_not_ready_raises_so_stripe_retries(self):
        p = pi()
        with self.assertRaises(StripeFeeNotReady):
            self.run_path(p, gw(p, bt=False))

    def test_unreadable_fee_goes_to_manual_review(self):
        p = pi()
        g = FakeGateway({"pi_123": {**expanded(p), "latest_charge": {"id": "ch_1", "balance_transaction": {
            "id": "txn_1", "amount": 100, "fee": "x"}}}})
        out = self.run_path(p, g)
        self.assertEqual(out.credits, [])
        self.assertIn("fee", out.manual_review)

    def test_forged_signature_rejected_before_any_lookup(self):
        p = pi()
        g = gw(p)
        raw, sig = _signed(_event(p), secret="whsec_attacker")
        from app.payments.stripe_pay import WebhookVerificationError
        with self.assertRaises(WebhookVerificationError):
            verify_webhook(raw, sig, [SECRET], use_stripe_lib=False)
        self.assertEqual(g.calls, [])


_HAVE_API = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "httpx", "pydantic"))


@unittest.skipUnless(_HAVE_API, "FastAPI/httpx not installed")
class HttpWebhookTests(unittest.TestCase):
    """POST /v1/webhooks/stripe through the real app + real StripeAdapter (fake gateway, in-memory world)."""

    def build(self, gateway):
        from fastapi.testclient import TestClient

        from app.api.adapters import StripeAdapter
        from app.api.main import create_app
        from app.api.testing import FakeWorld, make_services, make_settings

        world = FakeWorld()
        settings = make_settings(launch_phase="public", payouts_enabled=True)
        adapter = StripeAdapter(settings)
        adapter._gw = gateway
        svc = make_services(world, settings=settings, stripe=adapter)
        return world, TestClient(create_app(svc), raise_server_exceptions=False)

    def test_credit_net_of_fee_idempotent_and_not_withdrawable(self):
        # user id is known only after the world exists: build once, then shape the PI for that user.
        world, client = self.build(None)
        u = world.add_user("fb-user")
        p = pi(amount=2500, user=u["id"])
        g = gw(p, bt_amount=10_000, bt_fee=345)
        client.app.state.services.stripe._gw = g
        raw, sig = _signed(_event(p))
        for _ in range(2):                                            # redelivery is a no-op
            r = client.post("/v1/webhooks/stripe", content=raw, headers={"Stripe-Signature": sig})
            self.assertEqual(r.status_code, 200, r.text)
        fee = 25 * USD * 345 // 10_000
        self.assertEqual(world.fee_balance(u["id"]), 25 * USD - fee)
        self.assertIs(world.deposits["pi_123"]["withdrawable"], False)

    def test_fee_not_ready_is_5xx_and_credits_nothing(self):
        world, client = self.build(None)
        u = world.add_user("fb-user")
        p = pi(user=u["id"])
        client.app.state.services.stripe._gw = gw(p, bt=False)
        raw, sig = _signed(_event(p))
        r = client.post("/v1/webhooks/stripe", content=raw, headers={"Stripe-Signature": sig})
        self.assertGreaterEqual(r.status_code, 500)
        self.assertEqual(world.fee_balance(u["id"]), 0)

    def test_forged_signature_400(self):
        world, client = self.build(FakeGateway({}))
        raw, _ = _signed(_event(pi()))
        _, bad = _signed(_event(pi()), secret="whsec_attacker")
        r = client.post("/v1/webhooks/stripe", content=raw, headers={"Stripe-Signature": bad})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
