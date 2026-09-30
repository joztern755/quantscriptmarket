"""Stripe event -> ledger instruction mapping: credits, idempotency, refunds, disputes, MYR pro-rata."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.alerts.notifier import Severity  # noqa: E402
from app.payments.instructions import CreditInstruction, DebitInstruction  # noqa: E402
from app.errors import ExternalServiceError  # noqa: E402
from app.payments.stripe_pay import (  # noqa: E402
    DepositRecord,
    StripeFeeNotReady,
    StripeTopupConfig,
    handle_event,
    stripe_fee_micro,
)

CFG = StripeTopupConfig(env="test")                                   # prod default: fee passed through
CFG_ABSORB = StripeTopupConfig(env="test", stripe_fee_absorbed=True)
USER = "8c1f6a1e-0000-4000-8000-000000000001"


def pi(pi_id="pi_123", amount=5000, received=None, currency="usd", status="succeeded", meta=None):
    m = {"user_id": USER, "purpose": "fee_balance_topup", "idempotency": "tok_12345678", "env": "test",
         "credit_micro": str(amount * 10_000)}
    if meta is not None:
        m = meta
    return {"id": pi_id, "object": "payment_intent", "amount": amount,
            "amount_received": amount if received is None else received, "currency": currency,
            "status": status, "metadata": m}


def event(etype, obj, evt_id="evt_1", prev=None, livemode=False):
    data = {"object": obj}
    if prev is not None:
        data["previous_attributes"] = prev
    return {"id": evt_id, "object": "event", "type": etype, "livemode": livemode, "data": data}


MYR_META = {"user_id": USER, "purpose": "fee_balance_topup", "idempotency": "tok_12345678", "env": "test",
            "credit_micro": "50000000", "amount_minor": "23665", "fx_rate_applied": "4.733"}


def expanded(p, bt_amount=None, bt_fee=0, bt=True):
    """PI as returned by retrieve(expand=[latest_charge.balance_transaction]); bt in MYR (settlement currency)."""
    p = dict(p)
    p["latest_charge"] = {"id": "ch_1", "object": "charge",
                          "balance_transaction": ({"id": "txn_1", "amount": bt_amount or p["amount"] * 4,
                                                   "currency": "myr", "fee": bt_fee} if bt else None)}
    return p


class FakeGateway:
    def __init__(self, pis):
        self.pis = pis
        self.calls = []

    def retrieve_payment_intent(self, pi_id, expand=()):
        self.calls.append((pi_id, tuple(expand)))
        return self.pis[pi_id]


def gw(p, **kw):
    return FakeGateway({p["id"]: expanded(p, **kw)})

    def create_payment_intent(self, params, key):  # pragma: no cover
        raise AssertionError


class SucceededTests(unittest.TestCase):
    def ev(self, p, cfg=CFG, gateway="auto", **kw):
        g = gw(p) if gateway == "auto" else gateway
        return handle_event(event("payment_intent.succeeded", p), config=cfg, gateway=g, **kw)

    def test_usd_credit_zero_fee(self):
        out = self.ev(pi())
        self.assertEqual(len(out.credits), 1)
        c = out.credits[0]
        self.assertIsInstance(c, CreditInstruction)
        self.assertEqual((c.user_id, c.amount_micro, c.external_ref, c.idempotency_key),
                         (USER, 50_000_000, "pi_123", "stripe:pi_123"))
        self.assertEqual(c.debit_account, "stripe:clearing")
        self.assertEqual(c.credit_account, f"user:{USER}:fee_balance")
        self.assertFalse(c.withdrawable)
        self.assertEqual([a.kind for a in out.alerts], ["topup_credited"])

    def test_fee_pass_through_uses_actual_fee_share(self):
        # $50 charged; settled 235.00 MYR with 11.75 MYR fee (5%) -> fee $2.50, credit $47.50
        g = gw(pi(), bt_amount=23500, bt_fee=1175)
        out = self.ev(pi(), gateway=g)
        c = out.credits[0]
        self.assertEqual(c.amount_micro, 47_500_000)
        self.assertEqual((c.meta["gross_micro"], c.meta["fee_micro"]), (50_000_000, 2_500_000))
        self.assertEqual(g.calls, [("pi_123", ("latest_charge.balance_transaction",))])
        self.assertEqual(out.alerts[0].data["amount_micro"], 47_500_000)

    def test_fee_rounds_down(self):
        self.assertEqual(stripe_fee_micro(expanded(pi(amount=1001), bt_amount=3, bt_fee=1), 10_010_000), 3_336_666)

    def test_fee_not_ready_raises_for_retry(self):
        with self.assertRaises(StripeFeeNotReady):
            self.ev(pi(), gateway=gw(pi(), bt=False))
        g = FakeGateway({"pi_123": pi()})  # latest_charge not expanded
        with self.assertRaises(StripeFeeNotReady):
            self.ev(pi(), gateway=g)

    def test_pass_through_without_gateway_raises(self):
        with self.assertRaises(ExternalServiceError):
            self.ev(pi(), gateway=None)

    def test_bad_fee_goes_to_manual_review(self):
        out = self.ev(pi(), gateway=gw(pi(), bt_amount=100, bt_fee=100))
        self.assertEqual(out.instructions, [])
        self.assertIsNotNone(out.manual_review)

    def test_fee_lookup_override(self):
        out = self.ev(pi(), gateway=None, fee_lookup=lambda p: 1_750_000)
        self.assertEqual(out.credits[0].amount_micro, 48_250_000)

    def test_absorbed_mode_credits_gross_without_gateway(self):
        out = self.ev(pi(), cfg=CFG_ABSORB, gateway=None)
        self.assertEqual(out.credits[0].amount_micro, 50_000_000)

    def test_idempotent_redelivery(self):
        a = handle_event(event("payment_intent.succeeded", pi(), evt_id="evt_1"), config=CFG, gateway=gw(pi()))
        b = handle_event(event("payment_intent.succeeded", pi(), evt_id="evt_1"), config=CFG, gateway=gw(pi()))
        c = handle_event(event("payment_intent.succeeded", pi(), evt_id="evt_resend"), config=CFG, gateway=gw(pi()))
        self.assertEqual(a.credits[0].idempotency_key, b.credits[0].idempotency_key)
        self.assertEqual(a.credits[0].idempotency_key, c.credits[0].idempotency_key)
        self.assertEqual(a.credits[0].amount_micro, c.credits[0].amount_micro)

    def test_foreign_purpose_ignored(self):
        out = self.ev(pi(meta={"purpose": "other"}), gateway=None)
        self.assertEqual(out.instructions, [])
        self.assertEqual(out.ignored, "not a fee-balance top-up")

    def test_other_env_ignored(self):
        meta = dict(pi()["metadata"], env="prod")
        out = self.ev(pi(meta=meta), gateway=None)
        self.assertEqual(out.instructions, [])

    def test_unknown_event_ignored(self):
        out = handle_event(event("customer.created", {"id": "cus_1"}), config=CFG)
        self.assertEqual(out.ignored, "unhandled event type")
        self.assertEqual(out.instructions, [])

    def test_livemode_mismatch(self):
        cfg = StripeTopupConfig(env="prod", livemode_required=True)
        out = handle_event(event("payment_intent.succeeded", pi(), livemode=False), config=cfg, gateway=gw(pi()))
        self.assertEqual(out.instructions, [])
        self.assertEqual(out.alerts[0].severity, Severity.CRITICAL)

    def test_status_not_succeeded_goes_to_manual_review(self):
        p = pi(status="processing")
        out = self.ev(p, gateway=gw(p))
        self.assertEqual(out.instructions, [])
        self.assertIsNotNone(out.manual_review)
        self.assertEqual(out.alerts[0].kind, "payment_manual_review")

    def test_missing_user(self):
        p = pi(meta={"purpose": "fee_balance_topup"})
        out = self.ev(p, gateway=gw(p))
        self.assertEqual(out.instructions, [])
        self.assertIsNotNone(out.manual_review)

    def test_partial_received_credits_received_and_warns(self):
        p = pi(amount=5000, received=4000)
        out = self.ev(p, gateway=gw(p))
        self.assertEqual(out.credits[0].amount_micro, 40_000_000)
        self.assertIn(Severity.WARN, [a.severity for a in out.alerts])

    def test_myr_credits_locked_amount_minus_fee(self):
        p = pi(amount=23665, currency="myr", meta=MYR_META)
        out = self.ev(p, gateway=gw(p, bt_amount=23665, bt_fee=0))
        self.assertEqual(out.credits[0].amount_micro, 50_000_000)
        out = self.ev(p, gateway=gw(p, bt_amount=23665, bt_fee=710))  # 3% fee in sen
        self.assertEqual(out.credits[0].amount_micro, 50_000_000 - 50_000_000 * 710 // 23665)

    def test_myr_without_fx_lock_manual(self):
        meta = {k: v for k, v in MYR_META.items() if k != "amount_minor"}
        p = pi(amount=23665, currency="myr", meta=meta)
        out = self.ev(p, gateway=gw(p))
        self.assertEqual(out.instructions, [])
        self.assertIsNotNone(out.manual_review)

    def test_refetch_via_gateway_is_authoritative(self):
        # Event claims 5000 but Stripe (source of truth) says 1000 received.
        g = gw(pi(amount=1000))
        out = self.ev(pi(amount=5000), gateway=g)
        self.assertEqual(out.credits[0].amount_micro, 10_000_000)

    def test_refetch_not_succeeded(self):
        out = self.ev(pi(), gateway=gw(pi(status="requires_payment_method")))
        self.assertEqual(out.instructions, [])

    def test_payment_failed_info_alert(self):
        out = handle_event(event("payment_intent.payment_failed", pi(status="requires_payment_method")), config=CFG)
        self.assertEqual(out.instructions, [])
        self.assertEqual(out.alerts[0].kind, "topup_failed")
        self.assertEqual(out.alerts[0].user_id, USER)


def charge(ch_id="ch_1", amount=5000, refunded=0, currency="usd", meta="default", pi_id="pi_123"):
    m = pi()["metadata"] if meta == "default" else meta
    return {"id": ch_id, "object": "charge", "amount": amount, "amount_refunded": refunded, "currency": currency,
            "payment_intent": pi_id, "metadata": m}


class RefundTests(unittest.TestCase):
    def test_partial_then_full_refund_telescopes(self):
        e1 = event("charge.refunded", charge(refunded=2000), prev={"amount_refunded": 0, "refunded": False})
        e2 = event("charge.refunded", charge(refunded=5000), prev={"amount_refunded": 2000})
        d1 = handle_event(e1, config=CFG).debits[0]
        d2 = handle_event(e2, config=CFG).debits[0]
        self.assertIsInstance(d1, DebitInstruction)
        self.assertEqual((d1.amount_micro, d2.amount_micro), (20_000_000, 30_000_000))
        self.assertNotEqual(d1.idempotency_key, d2.idempotency_key)
        self.assertEqual(d1.idempotency_key, "stripe:refund:ch_1:2000")
        self.assertEqual(d1.debit_account, f"user:{USER}:fee_balance")
        self.assertEqual(d1.credit_account, "stripe:clearing")
        self.assertTrue(d1.may_go_negative)
        # redelivery -> same key
        self.assertEqual(handle_event(e1, config=CFG).debits[0].idempotency_key, d1.idempotency_key)

    def test_refund_alerts_critical_ops_and_user(self):
        out = handle_event(event("charge.refunded", charge(refunded=5000), prev={"amount_refunded": 0}), config=CFG)
        sev = {a.kind: a.severity for a in out.alerts}
        self.assertEqual(sev["stripe_refund_ops"], Severity.CRITICAL)
        self.assertEqual(sev["stripe_refund"], Severity.WARN)

    def test_myr_refunds_sum_exactly_to_credit(self):
        parts = [(0, 7001), (7001, 15000), (15000, 23665)]
        total = 0
        for prev, cum in parts:
            out = handle_event(event("charge.refunded", charge(amount=23665, refunded=cum, currency="myr", meta=MYR_META),
                                     prev={"amount_refunded": prev}), config=CFG)
            total += out.debits[0].amount_micro
        self.assertEqual(total, 50_000_000)

    def test_missing_previous_attributes_manual(self):
        out = handle_event(event("charge.refunded", charge(refunded=5000)), config=CFG)
        self.assertEqual(out.instructions, [])
        self.assertIsNotNone(out.manual_review)

    def test_refund_via_lookup_when_no_metadata(self):
        rec = DepositRecord(USER, "pi_123", "usd", 5000, 50_000_000)
        out = handle_event(event("charge.refunded", charge(refunded=1000, meta={}), prev={"amount_refunded": 0}),
                           config=CFG, lookup=lambda p: rec if p == "pi_123" else None)
        self.assertEqual(out.debits[0].amount_micro, 10_000_000)

    def test_refund_on_unrelated_charge_ignored(self):
        out = handle_event(event("charge.refunded", charge(refunded=1000, meta={"purpose": "other"}),
                                 prev={"amount_refunded": 0}), config=CFG)
        self.assertEqual(out.instructions, [])


def dispute(dp_id="dp_1", amount=5000, status="needs_response", currency="usd", pi_id="pi_123"):
    return {"id": dp_id, "object": "dispute", "amount": amount, "currency": currency, "charge": "ch_1",
            "payment_intent": pi_id, "reason": "fraudulent", "status": status, "metadata": {}}


class DisputeTests(unittest.TestCase):
    REC = DepositRecord(USER, "pi_123", "usd", 5000, 50_000_000)

    def lookup(self, pi_id):
        return self.REC if pi_id == "pi_123" else None

    def test_dispute_created_debits_and_pages(self):
        out = handle_event(event("charge.dispute.created", dispute()), config=CFG, lookup=self.lookup)
        d = out.debits[0]
        self.assertEqual((d.amount_micro, d.idempotency_key, d.kind), (50_000_000, "stripe:dispute:dp_1", "stripe_dispute"))
        ops = [a for a in out.alerts if a.user_id is None]
        self.assertEqual(ops[0].severity, Severity.CRITICAL)
        self.assertTrue(any(a.user_id == USER for a in out.alerts))

    def test_dispute_created_resolves_via_gateway(self):
        gw = FakeGateway({"pi_123": pi()})
        out = handle_event(event("charge.dispute.created", dispute()), config=CFG, gateway=gw)
        self.assertEqual(out.debits[0].user_id, USER)

    def test_dispute_unresolvable_manual_review(self):
        out = handle_event(event("charge.dispute.created", dispute()), config=CFG)
        self.assertEqual(out.instructions, [])
        self.assertEqual(out.alerts[0].severity, Severity.CRITICAL)

    def test_dispute_won_recredits(self):
        out = handle_event(event("charge.dispute.closed", dispute(status="won")), config=CFG, lookup=self.lookup)
        c = out.credits[0]
        self.assertEqual((c.amount_micro, c.idempotency_key), (50_000_000, "stripe:dispute_reinstated:dp_1"))

    def test_dispute_lost_no_instruction(self):
        out = handle_event(event("charge.dispute.closed", dispute(status="lost")), config=CFG, lookup=self.lookup)
        self.assertEqual(out.instructions, [])
        self.assertIn("debit stands", out.ignored)


if __name__ == "__main__":
    unittest.main()
