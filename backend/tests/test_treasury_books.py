"""REVIEW_MONEY M7(b), (c), (g): treasury readers (app.execution.treasury_books) and the reconcile bookings / checks
built on them (app.execution.reconcile). Stdlib only: fake Hyperliquid info client, fake Stripe gateway, fake ledger.

- M7(b) builder rewards claimed into the treasury are booked treasury:hl_usdc ← builder:hl_receivable (idempotent,
  cursor), only when the builder address is the treasury; receivable + unrecognised fees vs on-chain unclaimed.
- M7(g) treasury balance = perp (validator dex) + spot USDC + each trusted builder dex; bounded number of dex calls.
- M7(c) Stripe payouts booked clearing → bank (+ payout fee expense), reversals; clearing vs Stripe available+pending;
  ``not_configured`` without a reader, ``unsupported_currency`` for a non-USD settlement currency.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import FakeAlerts, FakeLedger, FakeReconcileRepo  # noqa: E402

from app.errors import ExternalServiceError, ValidationFailed  # noqa: E402
from app.execution.ports import BuilderClaim, StripePayoutMovement  # noqa: E402
from app.execution.reconcile import Reconciler  # noqa: E402
from app.execution.treasury_books import (  # noqa: E402
    HlBuilderRewardsReader,
    HlRewardsSchema,
    HlTreasuryReader,
    StripeBalanceReader,
    StripeCurrencyUnsupported,
    stripe_reader_from_settings,
)

USD = 1_000_000
BUILDER = "0x" + "b1" * 20
TREASURY = "0x" + "7e" * 20
HASH1 = "0x" + "ab" * 32
HASH2 = "0x" + "cd" * 32


class FakeInfo:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.referral: dict[str, Any] = {"builderRewards": "12.5"}
        self.updates: list[dict] = []
        self.perp: dict[str, str] = {"": "100.25"}
        self.spot: list[dict] = [{"coin": "USDC", "token": 0, "hold": "1.0", "total": "40.5"},
                                 {"coin": "HYPE", "token": 150, "hold": "0", "total": "3"}]

    def post(self, body: dict) -> Any:
        self.calls.append(("post", body.get("type"), body.get("user")))
        if body["type"] in ("referral", "referralAlt"):
            return self.referral
        if body["type"] == "spotClearinghouseState":
            return {"balances": self.spot}
        raise AssertionError(body)

    def iter_user_non_funding_ledger_updates(self, user: str, start_ms: int, end_ms: int, *, max_pages: int = 50):
        self.calls.append(("updates", user, start_ms, max_pages))
        return [u for u in self.updates if not isinstance(u["time"], int) or start_ms <= u["time"] <= end_ms]

    def clearinghouse_state(self, user: str, dex: str = "") -> dict:
        self.calls.append(("clearinghouse", user, dex))
        return {"marginSummary": {"accountValue": self.perp.get(dex, "0")}, "assetPositions": []}


def _claim(t: int, amount: str, h: str | None = HASH1, typ: str = "rewardsClaim") -> dict:
    return {"time": t, "hash": h, "delta": {"type": typ, "amount": amount}}


# ================================================================================================== readers
class BuilderRewardsReaderTest(unittest.TestCase):
    def test_unclaimed_claims_and_cumulative(self) -> None:
        info = FakeInfo()
        info.updates = [_claim(1_000, "5.25"), _claim(2_000, "1", HASH2),
                        {"time": 1_500, "hash": HASH2, "delta": {"type": "send", "amount": "99"}},   # not a claim
                        _claim(3_000, "2", "0x" + "0" * 64)]                                      # all-zero hash
        r = HlBuilderRewardsReader(info, BUILDER.upper().replace("0X", "0x"), treasury_address=BUILDER,
                                   now_ms=lambda: 10_000, since_ms=0)
        self.assertTrue(r.claims_into_treasury)
        self.assertEqual(r.unclaimed_builder_rewards_micro(), 12_500_000)
        claims = r.builder_reward_claims(0)
        self.assertEqual([(c.time_ms, c.amount_micro) for c in claims], [(1_000, 5_250_000), (2_000, 1_000_000),
                                                                         (3_000, 2_000_000)])
        self.assertEqual(claims[0].ref, f"{HASH1}:1000")
        self.assertEqual(claims[2].ref, "nohash:3000:2000000")
        self.assertEqual(r.cumulative_builder_rewards_micro(), 12_500_000 + 8_250_000)
        self.assertIn(("updates", BUILDER, 0, 20), info.calls)          # bounded pages

    def test_builder_not_the_treasury_is_not_booked(self) -> None:
        self.assertFalse(HlBuilderRewardsReader(FakeInfo(), BUILDER, treasury_address=TREASURY).claims_into_treasury)
        self.assertFalse(HlBuilderRewardsReader(FakeInfo(), "", treasury_address="").claims_into_treasury)
        with self.assertRaises(ValidationFailed):
            HlBuilderRewardsReader(FakeInfo(), "").unclaimed_builder_rewards_micro()

    def test_field_names_are_configurable(self) -> None:
        info = FakeInfo()
        info.referral = {"unclaimedBuilder": "3"}
        info.updates = [_claim(1_000, "7", typ="builderClaim")]
        old = dict(os.environ)
        try:
            os.environ.update({"HL_REWARDS_REFERRAL_TYPE": "referralAlt", "HL_REWARDS_UNCLAIMED_FIELD": "unclaimedBuilder",
                               "HL_REWARDS_CLAIM_TYPE": "builderClaim"})
            schema = HlRewardsSchema.from_settings(None)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(schema.claim_amount_field, "amount")
        r = HlBuilderRewardsReader(info, BUILDER, schema=schema, now_ms=lambda: 5_000)
        self.assertEqual(r.unclaimed_builder_rewards_micro(), 3 * USD)
        self.assertEqual([c.amount_micro for c in r.builder_reward_claims(0)], [7 * USD])
        # a Settings attribute wins over the environment
        class S:
            hl_rewards_claim_type = "fromSettings"
        self.assertEqual(HlRewardsSchema.from_settings(S()).claim_type, "fromSettings")

    def test_bad_shapes_raise(self) -> None:
        info = FakeInfo()
        info.referral = ["nope"]                                           # type: ignore[assignment]
        with self.assertRaises(ExternalServiceError):
            HlBuilderRewardsReader(info, BUILDER).unclaimed_builder_rewards_micro()
        info.updates = [{"time": "x", "hash": HASH1, "delta": {"type": "rewardsClaim", "amount": "1"}}]
        with self.assertRaises(ExternalServiceError):
            HlBuilderRewardsReader(info, BUILDER, now_ms=lambda: 5).builder_reward_claims(0)


class TreasuryReaderTest(unittest.TestCase):
    def test_sums_perp_spot_and_trusted_dexes(self) -> None:
        info = FakeInfo()
        info.perp.update({"xyz": "10", "abc": "0.000001"})
        r = HlTreasuryReader(info, TREASURY, dexes=lambda: ["xyz", "", "abc", "xyz"])
        self.assertEqual(r.treasury_usdc_micro(), 100_250_000 + 40_500_000 + 10 * USD + 1)
        self.assertEqual(r.last_breakdown, {"perp": 100_250_000, "spot_usdc": 40_500_000, "dex:abc": 1,
                                            "dex:xyz": 10 * USD})
        dex_calls = [c[2] for c in info.calls if c[0] == "clearinghouse"]
        self.assertEqual(dex_calls, ["", "abc", "xyz"])                    # one call per dex, validator dex once

    def test_bounded_before_any_call(self) -> None:
        info = FakeInfo()
        r = HlTreasuryReader(info, TREASURY, dexes=lambda: [f"d{i}" for i in range(5)], max_dexes=4)
        with self.assertRaises(ValidationFailed):
            r.treasury_usdc_micro()
        self.assertEqual(info.calls, [])

    def test_without_dex_provider_is_perp_plus_spot(self) -> None:
        self.assertEqual(HlTreasuryReader(FakeInfo(), TREASURY).treasury_usdc_micro(), 140_750_000)
        with self.assertRaises(ValidationFailed):
            HlTreasuryReader(FakeInfo(), "").treasury_usdc_micro()


class FakeStripeGateway:
    def __init__(self) -> None:
        self.balance: dict = {"available": [{"amount": 12_345, "currency": "usd"}],
                              "pending": [{"amount": 655, "currency": "usd"}]}
        self.txns: dict[str, list[dict]] = {"payout": [], "payout_failure": [], "payout_cancel": []}
        self.calls: list[dict] = []

    def retrieve_balance(self) -> dict:
        return self.balance

    def list_balance_transactions(self, params: dict) -> dict:
        self.calls.append(dict(params))
        rows = [t for t in self.txns[params["type"]] if t["created"] >= params["created[gte]"]]
        if params.get("starting_after"):
            ids = [t["id"] for t in rows]
            rows = rows[ids.index(params["starting_after"]) + 1:]
        page = rows[:params["limit"]]
        return {"object": "list", "data": page, "has_more": len(rows) > len(page)}


def _bt(i: int, typ: str, amount: int, fee: int = 0, created: int = 1_700_000_000, cur: str = "usd") -> dict:
    return {"id": f"txn_{typ[:3]}{i}", "type": typ, "amount": amount, "fee": fee, "net": amount - fee,
            "currency": cur, "created": created + i, "source": f"po_{i}"}


class StripeReaderTest(unittest.TestCase):
    def test_balance_available_plus_pending(self) -> None:
        self.assertEqual(StripeBalanceReader(FakeStripeGateway()).clearing_balance_micro(), 13_000 * 10_000)

    def test_payout_movements_paginated_and_signed(self) -> None:
        gw = FakeStripeGateway()
        gw.txns["payout"] = [_bt(i, "payout", -1_000 * (i + 1), fee=25 if i == 2 else 0) for i in range(5)]
        gw.txns["payout_failure"] = [_bt(9, "payout_failure", 3_000)]
        mv = StripeBalanceReader(gw, page_size=2).payout_movements(1_700_000_000_000)
        self.assertEqual(len(mv), 6)
        self.assertEqual([m.txn_id for m in mv][:2], ["txn_pay0", "txn_pay1"])
        self.assertEqual((mv[2].amount_micro, mv[2].fee_micro, mv[2].reversal), (3_000 * 10_000, 250_000, False))
        self.assertEqual((mv[-1].amount_micro, mv[-1].reversal, mv[-1].payout_id), (30_000_000, True, "po_9"))
        self.assertEqual(sum(1 for c in gw.calls if c["type"] == "payout"), 3)          # 5 rows, 2 per page
        self.assertTrue(all(c["created[gte]"] == 1_700_000_000 for c in gw.calls))

    def test_pagination_is_bounded(self) -> None:
        gw = FakeStripeGateway()
        gw.txns["payout"] = [_bt(i, "payout", -100) for i in range(10)]
        with self.assertRaises(ExternalServiceError):
            StripeBalanceReader(gw, page_size=2, max_pages=3).payout_movements(0)

    def test_non_usd_settlement_is_unsupported(self) -> None:
        with self.assertRaises(StripeCurrencyUnsupported):
            StripeBalanceReader(FakeStripeGateway(), currency="myr").clearing_balance_micro()
        gw = FakeStripeGateway()
        gw.balance["pending"].append({"amount": 5, "currency": "myr"})
        with self.assertRaises(StripeCurrencyUnsupported):
            StripeBalanceReader(gw).clearing_balance_micro()
        gw = FakeStripeGateway()
        gw.txns["payout"] = [_bt(1, "payout", -100, cur="eur")]
        with self.assertRaises(StripeCurrencyUnsupported):
            StripeBalanceReader(gw).payout_movements(0)

    def test_wrong_signs_raise(self) -> None:
        gw = FakeStripeGateway()
        gw.txns["payout"] = [_bt(1, "payout", 100)]
        with self.assertRaises(ExternalServiceError):
            StripeBalanceReader(gw).payout_movements(0)

    def test_from_settings(self) -> None:
        class S:
            stripe_secret_key = ""
        self.assertIsNone(stripe_reader_from_settings(S()))
        S.stripe_secret_key = "sk_test_x"
        S.stripe_settlement_currency = "MYR"
        r = stripe_reader_from_settings(S(), gateway_factory=lambda s: FakeStripeGateway())
        self.assertEqual(r.currency, "myr")


class StripeHttpGatewayBalanceTest(unittest.TestCase):
    def test_balance_endpoints(self) -> None:
        from app.payments.stripe_pay import StripeHttpGateway

        class Resp:
            status_code = 200

            def __init__(self, body): self.body = body
            def json(self): return self.body

        class Session:
            def __init__(self): self.gets = []

            def get(self, url, params=None, headers=None, timeout=None):
                self.gets.append((url, params, headers))
                return Resp({"object": "list", "data": [], "has_more": False} if "balance_transactions" in url
                            else {"available": [], "pending": []})

        s = Session()
        gw = StripeHttpGateway("sk_test_x", session=s)
        self.assertEqual(gw.retrieve_balance(), {"available": [], "pending": []})
        gw.list_balance_transactions({"type": "payout", "created[gte]": 5, "limit": 100})
        self.assertEqual(s.gets[1][0], "https://api.stripe.com/v1/balance_transactions")
        self.assertEqual(s.gets[1][1], [("type", "payout"), ("created[gte]", "5"), ("limit", "100")])
        self.assertEqual(s.gets[1][2]["Authorization"], "Bearer sk_test_x")
        with self.assertRaises(ValidationFailed):
            gw.list_balance_transactions({"expand[]": "data.source"})


# ================================================================================================== reconcile
class Pos:
    def positions(self, address, coins):
        return {}


class Const:
    def __init__(self, v: int) -> None:
        self.v = v

    def cumulative_builder_rewards_micro(self) -> int:
        return self.v


class FakeClaims:
    def __init__(self, claims: list[BuilderClaim], unclaimed: int, *, into_treasury: bool = True) -> None:
        self.claims = claims
        self.unclaimed = unclaimed
        self.claims_into_treasury = into_treasury
        self.since_ms = 1_000
        self.asked: list[int] = []

    def cumulative_builder_rewards_micro(self) -> int:
        return self.unclaimed + sum(c.amount_micro for c in self.claims)

    def unclaimed_builder_rewards_micro(self) -> int:
        return self.unclaimed

    def builder_reward_claims(self, since_ms: int) -> list[BuilderClaim]:
        self.asked.append(since_ms)
        return [c for c in self.claims if c.time_ms >= since_ms]


class CursorRepo(FakeReconcileRepo):
    def __init__(self, unrecognised: int = 0, builder_total: int = 0) -> None:
        super().__init__(builder_total=builder_total)
        self.cursors: dict[str, int] = {}
        self.unrecognised = unrecognised

    def get_cursor(self, name: str) -> int | None:
        return self.cursors.get(name)

    def set_cursor(self, name: str, ms: int) -> None:
        self.cursors[name] = max(self.cursors.get(name, 0), ms)

    def unrecognised_builder_fees_micro(self) -> int:
        return self.unrecognised


class Treasury:
    def __init__(self, v: int) -> None:
        self.v = v
        self.last_breakdown = {"perp": v - 5, "spot_usdc": 5}

    def treasury_usdc_micro(self) -> int:
        return self.v


class FakeStripeReader:
    def __init__(self, balance: int, movements: list[StripePayoutMovement], *, currency_ok: bool = True) -> None:
        self.balance = balance
        self.movements = movements
        self.currency_ok = currency_ok

    def _check(self) -> None:
        if not self.currency_ok:
            raise StripeCurrencyUnsupported("Stripe settlement currency is not USD", currency="myr")

    def clearing_balance_micro(self) -> int:
        self._check()
        return self.balance

    def payout_movements(self, since_ms: int) -> list[StripePayoutMovement]:
        self._check()
        return [m for m in self.movements if m.created_ms >= since_ms]


class ReconcileBookingsTest(unittest.TestCase):
    def _rec(self, ledger: FakeLedger, *, repo: CursorRepo | None = None, claims: FakeClaims | None = None,
             stripe: FakeStripeReader | None = None, treasury: int = 0, alerts: FakeAlerts | None = None) -> Reconciler:
        return Reconciler(repo=repo or CursorRepo(), positions=Pos(), builder_rewards=Const(0),
                          treasury=Treasury(treasury), ledger=ledger, alerts=alerts or FakeAlerts(),
                          builder_claims=claims, stripe=stripe)

    def test_not_configured_by_default(self) -> None:
        rep = self._rec(FakeLedger()).run("2026-10-02").as_dict()
        self.assertEqual((rep["stripe_clearing_status"], rep["builder_claims_status"]),
                         ("not_configured", "not_configured"))
        self.assertEqual(rep["treasury_breakdown"], {"perp": -5, "spot_usdc": 5})

    def test_builder_claims_booked_once_and_receivable_reconciled(self) -> None:
        led = FakeLedger()
        led.post_transaction(idempotency_key="bf:1", kind="builder_fee", memo="", created_by="t", lines=[
            _ln("builder:hl_receivable", 30 * USD), _ln("platform:revenue:builder", -30 * USD)])
        claims = FakeClaims([BuilderClaim(f"{HASH1}:5000", 5_000, 20 * USD)], unclaimed=12 * USD)
        repo = CursorRepo(unrecognised=2 * USD)
        alerts = FakeAlerts()
        rep = self._rec(led, repo=repo, claims=claims, treasury=20 * USD, alerts=alerts).run("2026-10-02")
        self.assertEqual((rep.builder_claims_status, rep.builder_claims_booked, rep.builder_claims_booked_micro),
                         ("ok", 1, 20 * USD))
        self.assertEqual(led.txs[f"builder_claim:{HASH1}:5000"][1], "builder_rewards_claim")
        self.assertEqual(led.balance("builder:hl_receivable"), 10 * USD)
        self.assertEqual(led.balance("treasury:hl_usdc"), 20 * USD)
        # receivable 10 + unrecognised 2 == on-chain unclaimed 12; the treasury check already sees the claim
        self.assertFalse(rep.builder_receivable_mismatch)
        self.assertFalse(rep.treasury_mismatch)
        self.assertEqual(repo.cursors["builder_claims"], 5_000)
        self.assertEqual(claims.asked, [0])                       # first run: reader default (1_000) − overlap
        rep2 = self._rec(led, repo=repo, claims=claims, treasury=20 * USD).run("2026-10-02")
        self.assertEqual(rep2.builder_claims_booked, 0)          # idempotent re-run
        self.assertEqual(led.balance("builder:hl_receivable"), 10 * USD)
        self.assertEqual(claims.asked[-1], 0)                    # 5_000 − 1 h overlap, floored at 0
        self.assertNotIn("reconciliation_mismatch", alerts.kinds())

    def test_receivable_mismatch_alerts(self) -> None:
        led = FakeLedger()
        led.post_transaction(idempotency_key="bf:1", kind="builder_fee", memo="", created_by="t", lines=[
            _ln("builder:hl_receivable", 30 * USD), _ln("platform:revenue:builder", -30 * USD)])
        alerts = FakeAlerts()
        rep = self._rec(led, claims=FakeClaims([], unclaimed=5 * USD, into_treasury=False), alerts=alerts).run("d")
        self.assertEqual(rep.builder_claims_status, "outside_treasury")
        self.assertTrue(rep.builder_receivable_mismatch)
        self.assertEqual(rep.builder_unclaimed_chain_micro, 5 * USD)
        self.assertIn("reconciliation_mismatch", alerts.kinds())
        self.assertEqual(len(led.txs), 1)                          # nothing booked

    def test_stripe_payouts_booked_and_clearing_reconciled(self) -> None:
        led = FakeLedger()
        led.post_transaction(idempotency_key="stripe:pi_1", kind="deposit", memo="", created_by="t", lines=[
            _ln("stripe:clearing", 100 * USD), _ln("user:u:fee_balance", -100 * USD)])
        mv = [StripePayoutMovement("txn_a", "po_a", 10_000, 60 * USD, 0),
              StripePayoutMovement("txn_b", "po_b", 20_000, 10 * USD, 1 * USD),
              StripePayoutMovement("txn_c", "po_a", 30_000, 5 * USD, 0, reversal=True)]
        repo = CursorRepo()
        rep = self._rec(led, repo=repo, stripe=FakeStripeReader(34 * USD, mv)).run("2026-10-02")
        self.assertEqual((rep.stripe_payouts_booked, rep.stripe_payouts_booked_micro), (3, 65 * USD))
        self.assertEqual(led.balance("stripe:clearing"), 34 * USD)
        self.assertEqual(led.balance("bank:payouts"), 65 * USD)
        self.assertEqual(led.balance("expense:stripe_fees"), 1 * USD)
        self.assertEqual({k: v[1] for k, v in led.txs.items() if k.startswith("stripe:pay")},
                         {"stripe:payout:txn_a": "stripe_payout", "stripe:payout:txn_b": "stripe_payout",
                          "stripe:payout_reversal:txn_c": "stripe_payout_reversal"})
        self.assertEqual(rep.stripe_clearing_status, "ok")
        self.assertEqual(repo.cursors["stripe_payouts"], 30_000)
        rep2 = self._rec(led, repo=repo, stripe=FakeStripeReader(34 * USD, mv)).run("2026-10-02")
        self.assertEqual(rep2.stripe_payouts_booked, 0)
        alerts = FakeAlerts()
        rep3 = self._rec(led, repo=repo, stripe=FakeStripeReader(40 * USD, mv), alerts=alerts).run("2026-10-02")
        self.assertEqual(rep3.stripe_clearing_status, "mismatch")
        self.assertIn("reconciliation_mismatch", alerts.kinds())

    def test_reversal_with_fee_is_left_to_ops(self) -> None:
        led = FakeLedger()
        alerts = FakeAlerts()
        mv = [StripePayoutMovement("txn_r", "po_r", 1, 5 * USD, 1, reversal=True)]
        rep = self._rec(led, stripe=FakeStripeReader(0, mv), alerts=alerts).run("d")
        self.assertEqual(rep.stripe_payouts_booked, 0)
        self.assertIn("stripe_payout_unbooked", alerts.kinds())

    def test_unsupported_currency(self) -> None:
        led = FakeLedger()
        alerts = FakeAlerts()
        rep = self._rec(led, stripe=FakeStripeReader(0, [], currency_ok=False), alerts=alerts).run("d")
        self.assertEqual(rep.stripe_clearing_status, "unsupported_currency")
        self.assertEqual(rep.errors, [])
        self.assertIn("stripe_clearing_unsupported_currency", alerts.kinds())
        self.assertEqual(led.txs, {})


def _ln(code: str, amount: int):
    from app.execution.ports import LedgerLine

    return LedgerLine(code, amount)


if __name__ == "__main__":
    unittest.main()
