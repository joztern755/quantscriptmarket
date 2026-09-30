"""REVIEW_AUTH_API F1 — user-triggered Hyperliquid calls must not starve the executor.

Reproduce → blocked:
* ``POST /deposits/usdc/confirm {"time_ms": 1600000000000}`` used to download the treasury's whole ledger inline, on
  the executor's shared IP; now it makes NO Hyperliquid call, records a scan request with the lookback clamped to
  max(now − 48 h, the wallet's verification time) and returns the credits deposits-scan already booked;
  ``UsdcAdapter.detect`` itself clamps to 48 h;
* ``GET /positions`` is cached per user for 15 s, bounded (≤ 5 addresses, trusted dexes only, ≤ 15 calls);
* API ``/info`` calls go through the shared DB budget (``app.hl.budget``) in the low-priority pool with a short wait
  and its own egress key; ``app_api`` can charge ``hl_rate_budget`` (migration 0012);
* the executor has its own subnet + Cloud NAT + static IP (infra), and its own ``HL_EGRESS_KEY``.
FastAPI parts are skipped when FastAPI/httpx are missing; DB parts need AIJALON_TEST_DATABASE_URL (through 0012).
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import HlLimits  # noqa: E402
from app.hl.budget import POOL_JOBS, BudgetHook, Charge, HlBudgetExhausted  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DB_URL = os.environ.get("AIJALON_TEST_DATABASE_URL", "")


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def _psql_ok() -> bool:
    try:
        return subprocess.run(["psql", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


HTTP = _have("fastapi") and _have("httpx")
RUN_DB = bool(DB_URL) and _psql_ok()


class _Refuse:
    def try_acquire(self, weight, pool=POOL_JOBS, *, force=False):
        return Charge(granted=False)

    def acquire_wait(self, weight, pool, *, deadline, monotonic, sleep):
        while monotonic() < deadline:
            sleep(1.0)
        return False


class BudgetHookForApiTest(unittest.TestCase):
    def test_api_pool_gives_up_fast(self) -> None:
        clock = [0.0]
        hook = BudgetHook(_Refuse(), HlLimits(), default_pool=POOL_JOBS, max_wait_seconds=2.0,
                          monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        with self.assertRaises(HlBudgetExhausted):
            hook.before({"type": "clearinghouseState"})
        self.assertLessEqual(clock[0], 2.0)


class InfraEgressSplitTest(unittest.TestCase):
    def test_executor_has_its_own_nat_and_subnet(self) -> None:
        env = (ROOT / "infra/gcp/env.sh").read_text()
        boot = (ROOT / "infra/gcp/bootstrap.sh").read_text()
        for name in ("EXEC_SUBNET:=", "EXEC_SUBNET_RANGE:=", "EXEC_NAT:=", "EXEC_NAT_IP_NAME:="):
            self.assertIn(name, env)
        self.assertIn('gcloud compute networks subnets create "${EXEC_SUBNET}"', boot)
        self.assertIn('gcloud compute routers nats create "${EXEC_NAT}"', boot)
        self.assertIn('--nat-custom-subnet-ip-ranges="${EXEC_SUBNET}" --nat-external-ip-pool="${EXEC_NAT_IP_NAME}"',
                      boot)
        self.assertIn('--nat-custom-subnet-ip-ranges="${RUN_SUBNET}" --nat-external-ip-pool="${NAT_IP_NAME}"', boot)
        ex = (ROOT / "infra/gcp/run/executor.service.yaml").read_text()
        api = (ROOT / "infra/gcp/run/api.service.yaml").read_text()
        self.assertIn('"subnetwork":"${EXEC_SUBNET}"', ex)
        self.assertIn('"subnetwork":"${RUN_SUBNET}"', api)
        self.assertIn('{name: HL_EGRESS_KEY, value: "executor"}', ex)
        self.assertIn('{name: HL_EGRESS_KEY, value: "api"}', api)
        self.assertIn("EXEC_SUBNET", (ROOT / "infra/gcp/deploy.sh").read_text())


@unittest.skipUnless(HTTP, "needs fastapi + httpx")
class ApiHlTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app.api.main import create_app
        from app.api.routers import positions as pos_mod
        from app.api.testing import FakeWorld, make_services

        pos_mod._cache.clear()
        self.world = FakeWorld()
        self.svc = make_services(self.world)
        self.c = TestClient(create_app(self.svc), raise_server_exceptions=False)
        self.user = self.world.add_user("fb-user")
        self.hl_calls: list = []

    def login(self) -> dict:
        tok = f"tok-{uuid.uuid4().hex[:6]}"
        self.svc.auth.add(tok, "fb-user", auth_time=self.world.now)
        return {"Authorization": f"Bearer {tok}"}

    def test_confirm_makes_no_hl_call_and_clamps_lookback(self) -> None:
        class NoHl:
            def __getattr__(inner, name):  # noqa: N805
                raise AssertionError(f"usdc.{name} must not be called by confirm")
        self.svc.usdc = NoHl()
        w = "0x" + "1" * 40
        self.world.add_wallet(self.user["id"], w)
        self.world.wallets[w]["verified_at"] = self.world.now - timedelta(days=10)
        self.world.credit(self.user["id"], 25_000_000)
        r = self.c.post("/v1/deposits/usdc/confirm", headers={**self.login(), "Idempotency-Key": str(uuid.uuid4())},
                        json={"time_ms": 1_600_000_000_000})
        self.assertEqual(r.status_code, 200, r.text)
        req = self.world.deposit_scan_requests[self.user["id"]]
        self.assertEqual(req["since"], self.world.now - timedelta(hours=48))      # not 2020
        self.assertEqual(len(r.json()["credited"]), 1)
        # verified recently → since = verification time
        self.world.wallets[w]["verified_at"] = self.world.now - timedelta(hours=3)
        self.c.post("/v1/deposits/usdc/confirm", headers={**self.login(), "Idempotency-Key": str(uuid.uuid4())},
                    json={})
        self.assertEqual(self.world.deposit_scan_requests[self.user["id"]]["since"],
                         self.world.now - timedelta(hours=48))   # unserved request keeps the earlier since

    def test_positions_cached_bounded_trusted_only(self) -> None:
        def ch(user, dex=""):
            self.hl_calls.append((user, dex))
            return {"assetPositions": []}
        self.svc.hl.clearinghouse_state = ch
        for i in range(8):
            self.world.add_wallet(self.user["id"], "0x" + f"{i + 1:040x}")
        st, ver = self.world.add_strategy("evil", markets=("evil:PUMP", "xyz:SILVER", "BTC"))
        self.world.subscriptions["s1"] = {"id": "s1", "user_id": self.user["id"], "strategy_id": st["id"],
                                          "strategy_version_id": ver["id"], "status": "active",
                                          "trading_address": "0x" + f"{1:040x}", "created_at": self.world.now}
        r = self.c.get("/v1/positions", headers=self.login())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLessEqual(len(self.hl_calls), 15)
        self.assertNotIn("evil", {d for _, d in self.hl_calls})
        self.assertLessEqual(len({u for u, _ in self.hl_calls}), 5)
        self.assertGreaterEqual(len(r.json()["unavailable"]), 3)
        n = len(self.hl_calls)
        self.assertEqual(self.c.get("/v1/positions", headers=self.login()).status_code, 200)
        self.assertEqual(len(self.hl_calls), n)                                  # served from the 15 s cache

    def test_detect_clamps_to_48h(self) -> None:
        from app.api.adapters import UsdcAdapter

        seen = []

        class Hl:
            def ledger_updates(self, user, start_ms):
                seen.append(start_ms)
                return []
        s = dataclasses.replace(self.svc.settings, treasury_address="0x" + "7" * 40)
        try:
            UsdcAdapter(s, Hl()).detect(senders=["0x" + "1" * 40], since_ms=1_600_000_000_000)
        except Exception:  # noqa: BLE001 - app.hl.deposits may reject the empty scan; only the start matters
            pass
        self.assertTrue(seen)
        self.assertGreaterEqual(seen[0], int((time.time() - 48 * 3600 - 60) * 1000))

    def test_hl_adapter_charges_shared_budget(self) -> None:
        from app.api.adapters import API_HL_MAX_WAIT_SECONDS, HlInfoAdapter

        with_db = HlInfoAdapter(self.svc.settings, db=self.svc.db).client()
        self.assertIsInstance(with_db.rate_hook, BudgetHook)
        self.assertEqual(with_db.rate_hook.default_pool, POOL_JOBS)
        self.assertEqual(with_db.rate_hook.max_wait, API_HL_MAX_WAIT_SECONDS)
        self.assertIsNone(HlInfoAdapter(self.svc.settings).client().rate_hook)


if RUN_DB:
    from test_api_store_db import ApiRoleRunner  # noqa: E402


@unittest.skipUnless(RUN_DB, "needs AIJALON_TEST_DATABASE_URL (migrated through 0012) and psql")
class ApiHlDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.api.store import SqlStore

        cls.api = ApiRoleRunner(DB_URL, "app_api")
        cls.store = SqlStore()
        tag = uuid.uuid4().hex[:8]
        cls.uid = str(cls.store.create_user(cls.api, firebase_uid=f"fbH{tag}", email=f"h{tag}@x.io",
                                            display_name="H", referral_code=f"H{tag}", referred_by=None,
                                            mfa_enrolled=True)["id"])

    def test_scan_request_upsert(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.store.request_deposit_scan(self.api, self.uid, since=now - timedelta(hours=10), now=now)
        self.store.request_deposit_scan(self.api, self.uid, since=now - timedelta(hours=2), now=now)
        row = self.api.fetchall("""SELECT since, served_at FROM deposit_scan_requests
                                    WHERE user_id = CAST(:u AS uuid)""", {"u": self.uid})[0]
        self.assertEqual(datetime.fromisoformat(row["since"]), now - timedelta(hours=10))   # earliest unserved wins
        self.assertIsNone(row["served_at"])

    def test_api_can_charge_its_own_budget_key(self) -> None:
        from app.hl.budget import HlRateBudget

        key = "api" + uuid.uuid4().hex[:6]
        b = HlRateBudget(self.api, HlLimits(egress_key=key, budget_weight_per_minute=100, tick_reserve_per_minute=0))
        c = b.try_acquire(20, POOL_JOBS)
        self.assertTrue(c.granted)
        self.assertFalse(c.fail_open)                               # really accounted (not "table unavailable")
        self.assertEqual(b.usage()["spent_jobs"], 20)


if __name__ == "__main__":
    unittest.main()
