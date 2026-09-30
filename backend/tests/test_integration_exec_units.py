"""Execution integration — unit level (stdlib only, no DB): closing flow (SPEC §12), mid-tick cancel, cloid single
source, market-snapshot mapping, builder-fee key, job helpers (sandbox client, weights, jitter salt), and the API's
job entrypoint contract."""
from __future__ import annotations

import inspect
import io
import json
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import BAR, SILVER, TRUSTED_DEXES, World, make_signal, make_sub  # noqa: E402

from app.errors import ExternalServiceError, ValidationFailed  # noqa: E402
from app.execution import executor as ex_mod  # noqa: E402
from app.execution.ports import Flags  # noqa: E402

UTC = timezone.utc


def closing_world(szi: str = "100", **cfg):
    w = World(subs=[make_sub(1, status="closing")], signals=[make_signal(weight=2)], **cfg)
    w.ex.set_position("0xmaster1", SILVER, szi)
    return w


class ClosingFlowTest(unittest.TestCase):
    def test_closes_reduce_only_regardless_of_signal_then_cancels(self):
        w = closing_world()
        r = w.tick()
        self.assertEqual(r.closing_seen, 1)
        self.assertEqual(len(w.ex.placed), 1)
        o = w.ex.placed[0]
        self.assertEqual((o["is_buy"], o["reduce_only"], o["sz"]), (False, True, Decimal("100")))
        self.assertEqual(w.ex.pos[("0xmaster1", SILVER)], 0)
        self.assertEqual(w.subs.subs["sub1"].status, "closing")          # flat is confirmed on the next tick
        r = w.tick()
        self.assertEqual(r.closings_completed, 1)
        self.assertEqual(w.subs.subs["sub1"].status, "cancelled")
        self.assertIn("positions_closed", w.alerts.kinds())
        self.assertEqual(len(w.ex.placed), 1)
        r = w.tick()
        self.assertEqual((r.closing_seen, len(w.ex.placed)), (0, 1))

    def test_short_position_is_bought_back(self):
        w = closing_world("-7.5")
        w.tick()
        o = w.ex.placed[0]
        self.assertEqual((o["is_buy"], o["reduce_only"], o["sz"]), (True, True, Decimal("7.5")))

    def test_already_flat_cancels_without_orders(self):
        w = closing_world("0")
        r = w.tick()
        self.assertEqual((r.closings_completed, len(w.ex.placed)), (1, 0))
        self.assertEqual(w.subs.subs["sub1"].status, "cancelled")

    def test_residual_after_max_attempts_alerts_and_keeps_closing(self):
        w = closing_world("100", max_attempts_per_bar=3)
        w.ex.script[SILVER] = ["partial:0.5"] * 10
        for _ in range(6):
            w.tick(advance_seconds=5)
        self.assertEqual(len(w.ex.placed), 3)                               # bounded within one retry epoch
        self.assertEqual(w.subs.subs["sub1"].status, "closing")
        self.assertIn("closing_residual", w.alerts.kinds())
        alert = next(a for a in w.alerts.items if a.kind == "closing_residual")
        self.assertEqual((alert.severity, alert.user_id), ("warn", "user1"))
        # the next retry epoch tries again
        w.tick(advance_seconds=3600)
        self.assertEqual(len(w.ex.placed), 4)

    def test_market_kill_switch_and_global_kill_stop_closing_too(self):
        w = closing_world()
        w.flags.value = Flags(trusted_dexes=TRUSTED_DEXES, kill_switch_global=True)
        w.tick()
        self.assertEqual(w.ex.placed, [])
        w.flags.value = Flags(trusted_dexes=TRUSTED_DEXES, killed_markets=frozenset({SILVER}))
        r = w.tick()
        self.assertEqual((w.ex.placed, r.market_killed), ([], 1))
        self.assertEqual(w.subs.subs["sub1"].status, "closing")

    def test_unknown_order_of_an_earlier_bar_blocks_closing_until_resolved(self):
        w = World(signals=[make_signal(weight=1)])
        w.ex.script[SILVER] = ["raise_after"]
        w.tick()
        self.assertEqual(w.subs.orders_of("sub1")[0].status, "unknown")
        w.subs.subs["sub1"] = replace(w.subs.subs["sub1"], status="closing")
        r = w.tick()   # resolves the unknown order (it filled) before closing the resulting position
        self.assertEqual(w.subs.orders_of("sub1")[0].status, "filled")
        self.assertEqual(r.orders_placed, 1)
        self.assertTrue(w.ex.placed[-1]["reduce_only"])
        self.assertEqual(w.ex.pos[("0xmaster1", SILVER)], 0)

    def test_breaker_blocks_closing(self):
        w = closing_world()
        w.subs.rejections["sub1"] = 3
        r = w.tick()
        self.assertEqual((w.ex.placed, r.breaker_open), ([], 1))
        self.assertIn("closing_blocked_breaker", w.alerts.kinds())

    def test_leave_is_never_touched_and_mid_tick_cancel_is_honoured(self):
        w = World()
        real_due = w.subs.due_subscriptions

        def due_then_cancel(*a, **k):   # the API cancels ("leave") between the fetch and the lock
            out = real_due(*a, **k)
            w.subs.subs["sub1"] = replace(w.subs.subs["sub1"], status="cancelled")
            return out

        w.subs.due_subscriptions = due_then_cancel
        w.tick()
        self.assertEqual(w.ex.placed, [])


class CloidSingleSourceTest(unittest.TestCase):
    def test_executor_cloid_is_hl_client_cloid(self):
        from app.hl import client

        self.assertIs(ex_mod.CLOID_PREFIX, client.CLOID_PREFIX)
        self.assertEqual(ex_mod.make_cloid("s1", BAR, SILVER, 2), client.make_cloid("s1", BAR, f"{SILVER}|2"))
        self.assertTrue(client.is_platform_cloid(ex_mod.make_cloid("s1", BAR, SILVER, 0)))
        with self.assertRaises(ValueError):
            ex_mod.make_cloid("s1", BAR, SILVER, -1)

    def test_fills_attribution_recognises_executor_cloids(self):
        from app.hl.fills import attribute_fills

        cloid = ex_mod.make_cloid("sub-9", BAR, SILVER, 0)
        raw = {"coin": SILVER, "px": "30", "sz": "1", "side": "B", "time": 1, "startPosition": "0", "dir": "Open Long",
               "closedPnl": "0", "fee": "0.01", "feeToken": "USDC", "builderFee": "0.003", "tid": 7, "oid": 8,
               "cloid": cloid, "hash": "0x0", "crossed": True}
        res = attribute_fills([raw], trading_address="0x" + "1" * 40, cloid_to_subscription={cloid: "sub-9"})
        self.assertEqual([a.subscription_id for a in res.attributed], ["sub-9"])


class SnapshotMappingTest(unittest.TestCase):
    def test_round_trip_and_catalog_adapter(self):
        from app.execution.wiring import CatalogMarketData, from_risk_snapshot, to_risk_snapshot
        from app.hl.fake import FakeInfo
        from app.hl.readers import HlMarketData

        src = HlMarketData(FakeInfo())
        md = CatalogMarketData(src)
        snap = md.snapshot(SILVER)
        risk = src.catalog([SILVER]).to_snapshot(SILVER)
        self.assertEqual(to_risk_snapshot(snap), risk)
        self.assertEqual(from_risk_snapshot(risk), snap)
        legacy = HlMarketData(FakeInfo()).snapshot(SILVER)   # the readers' snapshot agrees (except fetch time)
        self.assertEqual(replace(legacy, as_of=snap.as_of), snap)
        self.assertIsNone(md.snapshot("xyz:NOPE"))
        self.assertIsNone(src.snapshot("xyz:NOPE"))
        # single mapping: HlMarketData.snapshot delegates to wiring.from_risk_snapshot (no second copy)
        import inspect
        from unittest import mock

        from app.execution import wiring
        self.assertNotIn("day_notional_volume_micro", inspect.getsource(HlMarketData.snapshot))
        with mock.patch.object(wiring, "from_risk_snapshot", wraps=wiring.from_risk_snapshot) as spy:
            self.assertEqual(replace(src.snapshot(SILVER), as_of=snap.as_of), snap)
        self.assertEqual(spy.call_count, 1)

    def test_planner_treats_closing_as_reduce_only(self):
        from app.execution.ports import PlanInput, Position
        from app.execution.wiring import RiskPlanner
        from test_execution_fakes import FakeClock, FakeMarket

        clock = FakeClock(BAR)
        snap = FakeMarket(clock).snapshot(SILVER)
        sub = make_sub(1, status="closing")
        pos = Position(SILVER, Decimal("10"), 300_000_000)
        plan = RiskPlanner().plan(PlanInput(subscription=sub, coin=SILVER, weight_bps=20_000, position=pos,
                                            snapshot=snap, flags=Flags(trusted_dexes=TRUSTED_DEXES), reduce_only_mode=True, now=BAR))
        self.assertTrue(all(leg.reduce_only or not leg.is_buy for leg in plan.legs))
        plan0 = RiskPlanner().plan(PlanInput(subscription=sub, coin=SILVER, weight_bps=0, position=pos, snapshot=snap,
                                             flags=Flags(trusted_dexes=TRUSTED_DEXES), reduce_only_mode=True, now=BAR))
        self.assertEqual(len(plan0.legs), 1)
        self.assertTrue(plan0.legs[0].reduce_only and plan0.legs[0].close_position and not plan0.legs[0].is_buy)


class SettlementKeyTest(unittest.TestCase):
    def test_builder_fee_key_includes_trading_address(self):
        from app.execution.settlement import builder_fee_key

        a, b = "0x" + "AB" * 20, "0x" + "cd" * 20
        self.assertEqual(builder_fee_key(a, "123"), f"bf:{a.lower()}:123")
        self.assertNotEqual(builder_fee_key(a, "123"), builder_fee_key(b, "123"))
        with self.assertRaises(ValueError):
            builder_fee_key("", "1")


class JobHelpersTest(unittest.TestCase):
    def test_weight_to_bps_truncates_and_caps(self):
        from app.execution.jobs import weight_to_bps

        self.assertEqual(weight_to_bps(0.33333, 2), 3333)
        self.assertEqual(weight_to_bps(-0.33339, 2), -3333)
        self.assertEqual(weight_to_bps(2, 2), 20_000)
        with self.assertRaises(ValidationFailed):
            weight_to_bps(2.0001, 2)
        with self.assertRaises(ValidationFailed):
            weight_to_bps(float("nan"), 2)
        with self.assertRaises(ValidationFailed):
            weight_to_bps(True, 2)

    def test_jitter_salt(self):
        from app.config import get_settings
        from app.execution.jobs import jitter_salt

        s = replace(get_settings(), env="test", audit_pepper_b64="")
        self.assertEqual(len(jitter_salt(s)), 32)
        self.assertNotEqual(jitter_salt(s), jitter_salt(replace(s, audit_pepper_b64="cGVwcGVyLXBlcHBlci1wZXBwZXItcGVwcGVy")))
        with self.assertRaises(RuntimeError):
            jitter_salt(replace(s, env="prod"))

    def test_sandbox_client_request_and_auth(self):
        from app.execution.jobs import SandboxClient

        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen.update(url=req.full_url, headers=dict(req.header_items()), body=json.loads(req.data), timeout=timeout)
            return Resp(json.dumps({"weights": {SILVER: 1.0}, "code_hash": "h"}).encode())

        c = SandboxClient("https://sandbox.internal/", "secret-secret-secret", require_id_token=True,
                          token_provider=lambda aud: "tok-for-" + aud, opener=opener)
        out = c.run("src", {SILVER: []}, now_ms=123)
        self.assertEqual(out["weights"], {SILVER: 1.0})
        self.assertEqual(seen["url"], "https://sandbox.internal/run")
        self.assertEqual(seen["headers"]["X-sandbox-secret"], "secret-secret-secret")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer tok-for-https://sandbox.internal")
        self.assertEqual(seen["body"], {"source": "src", "bars": {SILVER: []}, "now_ms": 123})
        prod = SandboxClient("https://s", "x" * 20, require_id_token=True, token_provider=lambda aud: None,
                             opener=opener)
        with self.assertRaises(ExternalServiceError):
            prod.run("src", {}, now_ms=1)
        dev = SandboxClient("https://s", "x" * 20, require_id_token=False, token_provider=lambda aud: None,
                            opener=opener)
        dev.run("src", {}, now_ms=1)
        self.assertNotIn("Authorization", seen["headers"])
        with self.assertRaises(ExternalServiceError):
            SandboxClient("", "x", require_id_token=False).run("s", {}, now_ms=1)


class ExecutorNotifierPolicyTest(unittest.TestCase):
    """SPEC §12 email volume policy: the executor/settlement Notifier never emails USERS (the delivery worker does,
    from the `alerts` rows, with mutes + confirmed contacts); ops email / Telegram paging stay."""

    def test_build_notifier_emails_ops_only(self):
        from types import SimpleNamespace
        from unittest import mock

        from app.alerts import notifier as n
        from app.alerts.user_sinks import NoUserEmailContacts
        from app.execution import jobs
        from app.execution.pg import PgDatabase

        sent: list[tuple[str, str]] = []
        rows: list[str] = []

        class Provider:
            name = "fake"

            def __init__(self, *a, **k):
                pass

            def send(self, to, subject, text):
                sent.append((to, subject))

        class Runner:
            def fetchall(self, sql, params=None):
                rows.append(sql)
                return []

        settings = SimpleNamespace(email_provider_api_key="k", email_from="alerts@aijalon.trade",
                                   telegram_bot_token="", telegram_ops_chat_id="", ops_emails=("ops@aijalon.test",))
        with mock.patch.object(n, "ResendProvider", Provider):
            notif = jobs.build_notifier(settings, PgDatabase(Runner()), dedupe_store=n.InMemoryDedupeStore())
        self.assertIsInstance(notif.contacts, NoUserEmailContacts)
        notif.notify(n.Alert(kind="subscription_past_due", severity=n.Severity.WARN, user_id="u1", data={"x": 1}))
        self.assertEqual(sent, [])                                    # warn user alert: in-app row only
        self.assertTrue(any("INSERT INTO alerts" in q for q in rows))
        notif.notify(n.Alert(kind="order_rejections_burst", severity=n.Severity.CRITICAL, user_id="u1",
                             data={"subscription": "s", "count": 3, "coin": "-", "last_reason": "r"}))
        self.assertEqual([to for to, _ in sent], ["ops@aijalon.test"])   # critical: ops only, never the user
        notif.notify(n.Alert(kind="execution_error", severity=n.Severity.WARN, data={"error": "X"}))
        self.assertEqual([to for to, _ in sent], ["ops@aijalon.test"] * 2)


class JobEntrypointContractTest(unittest.TestCase):
    def test_api_adapter_finds_every_job_and_calls_with_db_now(self):
        import ast
        import importlib

        from app.execution import jobs

        # read the contract from app/api/adapters.py without importing FastAPI (not installed everywhere)
        src = (Path(__file__).resolve().parents[1] / "app" / "api" / "adapters.py").read_text()
        node = next(n for n in ast.parse(src).body if isinstance(n, ast.AnnAssign)
                    and getattr(n.target, "id", "") == "JOB_ENTRYPOINTS")
        JOB_ENTRYPOINTS = ast.literal_eval(node.value)

        def _fn(module, name):
            try:
                f = getattr(importlib.import_module(module), name, None)
            except ImportError:
                return None
            return f if callable(f) else None

        for job in ("tick", "settle-daily", "reconcile", "referral-tiers"):
            fn = next(f for f in (_fn(m, n) for m, n in JOB_ENTRYPOINTS[job]) if f is not None)
            self.assertEqual(fn.__module__, "app.execution.jobs", job)
            params = inspect.signature(fn).parameters
            self.assertIn("db", params)
            self.assertIn("now", params)
        self.assertIn("settle_date", inspect.signature(jobs.settle_daily).parameters)
        self.assertTrue(callable(_fn("app.execution.jobs", "latest_reconciliation")))

    def test_settle_date_is_the_trading_day(self):
        from app.execution import jobs

        calls = []

        class FakeSettlement:
            def settle_daily(self, d, now):
                calls.append(d)
                from app.execution.settlement import SettlementReport

                return SettlementReport(settle_date=d.isoformat())

        class Rt:
            def settlement(self, db):
                return FakeSettlement()

        class Runner:
            def fetchall(self, sql, params=None):
                return [{"ok": True}]

        now = datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
        out = jobs.settle_daily(db=Runner(), now=now, settle_date=date(2026, 9, 30), runtime=Rt())
        self.assertEqual(calls[-1], date(2026, 10, 1))                 # cut-off = the next midnight
        self.assertEqual((out["business_day"], out["cutoff"]), ("2026-09-30", "2026-10-01T00:00:00+00:00"))
        jobs.settle_daily(db=Runner(), now=now, settle_date="2026-10-01", runtime=Rt())
        self.assertEqual(calls[-1], date(2026, 10, 1))                 # never a cut-off after now
        jobs.settle_daily(db=Runner(), now=now, runtime=Rt())
        self.assertEqual(calls[-1], date(2026, 10, 1))
        with self.assertRaises(ValueError):
            jobs.settle_daily(db=Runner(), now=datetime(2026, 10, 1), runtime=Rt())


if __name__ == "__main__":
    unittest.main()
