"""REVIEW_TRADING_KEYS F1 — trusted builder-dex allowlist (SPEC §12 "Trusted builder dexes").

Reproduce → blocked:
* pure policy (app.strategies.dexes): validator perps always trusted; unknown dex rejected; allowlist unreadable (None)
  → fail closed for every builder dex;
* executor pre-trade guard (app.execution.wiring.RiskPlanner): a coin on an attacker-named dex can NOT be entered or
  increased (target clamped to 0) but an existing position CAN be exited; None → only validator perps open;
* API (FastAPI + fakes; skipped when FastAPI/httpx are missing): creator create / upload rejected before any Hyperliquid
  call for an untrusted dex; listing proposal + approval re-check; admin add (one admin, step-up, audit) / remove
  (immediate; entries paused; validator dex cannot be removed);
* DB (AIJALON_TEST_DATABASE_URL, migrated through 0012): the launch seed, SqlStore add/remove/re-add as app_api,
  CHECK constraints, app_executor read-only, PgFlagRepo snapshot carries the allowlist and a removal shows up at once.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.errors import ValidationFailed  # noqa: E402
from app.execution.ports import Flags, PlanInput, Position  # noqa: E402
from app.execution.wiring import RiskPlanner  # noqa: E402
from app.strategies import dexes  # noqa: E402
from test_execution_fakes import BAR, SILVER, FakeClock, FakeMarket, make_sub, usd  # noqa: E402

EVIL = "evil:PUMP"


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


HTTP = _have("fastapi") and _have("httpx")
DB_URL = os.environ.get("AIJALON_TEST_DATABASE_URL", "")


def _psql_ok() -> bool:
    try:
        return subprocess.run(["psql", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


RUN_DB = bool(DB_URL) and _psql_ok()


# ================================================================================================ pure policy
class DexPolicyTest(unittest.TestCase):
    def test_launch_list_matches_spec(self) -> None:
        self.assertEqual(set(dexes.LAUNCH_TRUSTED_DEXES),
                         {"xyz", "flx", "vntl", "hyna", "km", "abcd", "cash", "para", "mkts", "io"})

    def test_dex_of(self) -> None:
        self.assertEqual(dexes.dex_of("BTC"), "")
        self.assertEqual(dexes.dex_of("xyz:SILVER"), "xyz")
        self.assertEqual(dexes.dex_of(EVIL), "evil")

    def test_validator_always_trusted_builder_needs_allowlist(self) -> None:
        trusted = frozenset({"", "xyz"})
        self.assertTrue(dexes.is_trusted_coin("BTC", trusted))
        self.assertTrue(dexes.is_trusted_coin("xyz:SILVER", trusted))
        self.assertFalse(dexes.is_trusted_coin(EVIL, trusted))
        self.assertEqual(dexes.untrusted_markets(["BTC", EVIL, "xyz:GOLD"], trusted), [EVIL])

    def test_unreadable_allowlist_fails_closed(self) -> None:
        self.assertTrue(dexes.is_trusted_coin("BTC", None))
        self.assertFalse(dexes.is_trusted_coin("xyz:SILVER", None))

    def test_require_trusted(self) -> None:
        dexes.require_trusted(["BTC", "xyz:SILVER"], frozenset({"", "xyz"}))
        with self.assertRaises(ValidationFailed) as cm:
            dexes.require_trusted(["BTC", EVIL], frozenset({"", "xyz"}))
        self.assertEqual(cm.exception.details["reason"], "untrusted_dex")
        self.assertEqual(cm.exception.details["dexes"], ["evil"])

    def test_normalize_dex(self) -> None:
        self.assertEqual(dexes.normalize_dex(" xyz: "), "xyz")
        for bad in ("", "XYZ", "1ab", "a" * 17, "x-y", None, "xyz:SILVER"):
            with self.assertRaises(ValidationFailed, msg=repr(bad)):
                dexes.normalize_dex(bad)

    def test_remove_validator_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            dexes.remove_dex(object(), "", by="admin:x", reason="nope nope")


# ================================================================================================ executor guard
class ExecutorGuardTest(unittest.TestCase):
    def _plan(self, coin: str, *, trusted, weight: int = 10_000, position_usd: int = 0):
        clock = FakeClock(BAR)
        market = FakeMarket(clock, prices={coin: "30"})
        market.sz_decimals[coin] = 2
        pos = Position.flat(coin) if not position_usd else Position(coin=coin, szi=Decimal(position_usd) / 30,
                                                                      notional_micro=usd(position_usd))
        inp = PlanInput(subscription=make_sub(markets=(coin,)), coin=coin, weight_bps=weight, position=pos,
                        snapshot=market.snapshot(coin), flags=Flags(trusted_dexes=trusted), reduce_only_mode=False,
                        now=BAR)
        return RiskPlanner().plan(inp)

    def test_trusted_builder_dex_trades(self) -> None:
        plan = self._plan(SILVER, trusted=frozenset({"", "xyz"}))
        self.assertEqual(plan.target_notional_micro, usd(10_000))
        self.assertTrue(plan.legs and not plan.legs[0].reduce_only)

    def test_attacker_dex_entry_blocked(self) -> None:
        """Reproduce F1: before the fix the guard read only the dex's own (attacker-controlled) oracle/volume/OI and
        planned a full-size entry. Now: nothing is opened."""
        plan = self._plan(EVIL, trusted=frozenset({"", "xyz"}))
        self.assertEqual(plan.target_notional_micro, 0)
        self.assertEqual(plan.legs, ())

    def test_attacker_dex_increase_blocked_but_exit_allowed(self) -> None:
        hold = self._plan(EVIL, trusted=frozenset({""}), weight=20_000, position_usd=5_000)
        self.assertEqual(hold.legs, ())                                          # no increase
        exit_ = self._plan(EVIL, trusted=frozenset({""}), weight=0, position_usd=5_000)
        self.assertEqual(len(exit_.legs), 1)
        self.assertTrue(exit_.legs[0].reduce_only and not exit_.legs[0].is_buy)  # full close still goes out

    def test_allowlist_not_loaded_fails_closed(self) -> None:
        self.assertEqual(self._plan(SILVER, trusted=None).legs, ())              # builder dex: blocked
        btc = self._plan("BTC", trusted=None)                                    # validator perp: still trades
        self.assertEqual(btc.target_notional_micro, usd(10_000))

    def test_flag_repo_fails_closed_without_table(self) -> None:
        from app.execution.pg import PgFlagRepo

        class Broken:
            def all(self, sql, **params):
                if "trusted_dexes" in sql:
                    raise RuntimeError("relation does not exist")
                return []

        self.assertIsNone(PgFlagRepo(Broken()).flags().trusted_dexes)

    def test_removed_dex_pauses_entries_immediately(self) -> None:
        self.assertTrue(self._plan(SILVER, trusted=frozenset({"", "xyz"})).legs)
        self.assertEqual(self._plan(SILVER, trusted=frozenset({""})).legs, ())  # next tick after removal


# ================================================================================================ API (FakeWorld)
@unittest.skipUnless(HTTP, "needs fastapi + httpx")
class DexApiTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from app.api.deps import api_config
        from app.api.main import create_app
        from app.api.testing import FakeWorld, make_services

        self.world = FakeWorld()
        self.svc = make_services(self.world)
        self.c = TestClient(create_app(self.svc), raise_server_exceptions=False)
        self.creator = self.world.add_user("fb-creator", role="creator")
        self.world.consents.append({"user_id": self.creator["id"], "doc": "creator_agreement",
                                    "doc_version": api_config(self.svc.settings).legal_versions["creator_agreement"],
                                    "doc_text_sha256": "0" * 64, "context": "creator", "strategy_id": None,
                                    "accepted_at": self.world.now})
        self.a1 = self.world.add_user("fb-admin1", role="admin", email="a1@aijalon.trade")
        self.a2 = self.world.add_user("fb-admin2", role="admin", email="a2@aijalon.trade")
        self.hl_calls: list = []
        self.svc.hl.unknown_coins = lambda coins: (self.hl_calls.append(list(coins)), [])[1]

    def login(self, uid: str) -> dict:
        tok = f"tok-{uid}-{uuid.uuid4().hex[:6]}"
        self.svc.auth.add(tok, uid, auth_time=self.world.now)
        return {"Authorization": f"Bearer {tok}"}

    def test_create_strategy_on_untrusted_dex_rejected_before_hl_call(self) -> None:
        body = {"slug": "evil-pump", "name": "Evil pump", "markets": [EVIL], "timeframe": "1d",
                "price_monthly_micro": 0, "profit_share_bps": 0}
        r = self.c.post("/v1/creator/strategies", headers=self.login("fb-creator"), json=body)
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("trusted allowlist", r.text)
        self.assertEqual(self.hl_calls, [])                    # never fetched the attacker's dex meta

        def reached(coins):
            self.hl_calls.append(list(coins))
            raise ValidationFailed("reached the Hyperliquid market check")
        self.svc.hl.unknown_coins = reached
        ok = dict(body, slug="silver-x", markets=["xyz:SILVER", "BTC"])
        r = self.c.post("/v1/creator/strategies", headers=self.login("fb-creator"), json=ok)
        self.assertIn("reached the Hyperliquid market check", r.text)   # trusted markets pass the dex check
        self.assertEqual(self.hl_calls, [["xyz:SILVER", "BTC"]])

    def test_admin_add_and_remove(self) -> None:
        h1 = self.login("fb-admin1")
        r = self.c.post("/v1/admin/dexes", headers=h1, json={"dex": "evil", "reason": "due diligence done"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(r.json()["active"])
        self.assertTrue(any(a["action"] == "dex.trust.add" and a["target"] == "dex:evil" for a in self.world.audit))
        self.assertEqual(self.c.post("/v1/admin/dexes", headers=h1, json={"dex": "evil", "reason": "again again"})
                         .status_code, 409)
        self.assertIn("evil", {d["dex"] for d in self.c.get("/v1/admin/dexes", headers=h1).json() if d["active"]})
        self.world.add_strategy("pump", in_house=False, owner=self.creator["id"], markets=(EVIL,), status="listed")
        r = self.c.post("/v1/admin/dexes/evil/remove", headers=h1, json={"reason": "manipulation seen"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("pump", r.json()["affected_strategies"])
        self.assertEqual(r.json()["markets_entries_paused"], [EVIL])
        self.assertTrue(any(a["kind"] == "trusted_dex_removed" and a["severity"] == "critical"
                            for a in self.world.alerts))
        self.assertTrue(any(a["action"] == "dex.trust.remove" for a in self.world.audit))
        self.assertEqual(self.c.post("/v1/admin/dexes/evil/remove", headers=h1, json={"reason": "again again"})
                         .status_code, 404)

    def test_listing_proposal_and_approval_recheck_allowlist(self) -> None:
        from app.api.routers import admin
        from app.errors import Conflict

        st, ver = self.world.add_strategy("pump", in_house=False, owner=self.creator["id"], markets=(EVIL,),
                                          status="review")
        with self.svc.db.begin() as conn:
            with self.assertRaises(Conflict):
                admin._require_trusted_or_conflict(conn, self.svc, [EVIL])
            admin._require_trusted_or_conflict(conn, self.svc, ["xyz:SILVER", "BTC"])
            with self.assertRaises(Conflict):     # the second admin's approval re-checks (dex removed meanwhile)
                admin._apply_change(conn, self.svc, None, {"kind": "strategy_list", "target": f"strategy:{st['id']}",
                                                           "payload": {"version_id": ver["id"]}})
        self.assertNotEqual(self.world.strategies[st["id"]]["status"], "listed")
        r = self.c.post(f"/v1/admin/strategies/{st['id']}/list", headers=self.login("fb-admin1"),
                        json={"version_id": ver["id"], "reason": "listing ok"})
        self.assertEqual(r.status_code, 409, r.text)

    def test_validator_dex_cannot_be_removed_and_non_admin_refused(self) -> None:
        h1 = self.login("fb-admin1")
        self.assertIn(self.c.post("/v1/admin/dexes/%20/remove", headers=h1, json={"reason": "remove validator"})
                      .status_code, (404, 422))
        self.assertEqual(self.c.post("/v1/admin/dexes", headers=self.login("fb-creator"),
                                     json={"dex": "evil", "reason": "not an admin"}).status_code, 403)

    def test_admin_add_requires_step_up(self) -> None:
        tok = "tok-stale"
        self.svc.auth.add(tok, "fb-admin1", auth_time=self.world.now - timedelta(hours=2))
        r = self.c.post("/v1/admin/dexes", headers={"Authorization": f"Bearer {tok}"},
                        json={"dex": "evil", "reason": "due diligence done"})
        self.assertEqual(r.status_code, 401, r.text)


# ================================================================================================ DB
if RUN_DB:
    from test_api_store_db import ApiRoleRunner  # noqa: E402


@unittest.skipUnless(RUN_DB, "needs AIJALON_TEST_DATABASE_URL (migrated through 0012) and psql")
class DexDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.api.store import SqlStore

        cls.api = ApiRoleRunner(DB_URL, "app_api")
        cls.exe = ApiRoleRunner(DB_URL, "app_executor")
        cls.store = SqlStore()
        cls.dex = "zz" + uuid.uuid4().hex[:8]

    def test_seeded_launch_allowlist(self) -> None:
        active = self.store.trusted_dexes(self.api)
        self.assertTrue(set(dexes.LAUNCH_TRUSTED_DEXES) | {""} <= active)

    def test_add_remove_readd_as_api_and_executor_sees_it(self) -> None:
        from app.execution.pg import PgDatabase, PgFlagRepo

        repo = PgFlagRepo(PgDatabase(self.exe))
        self.assertNotIn(self.dex, repo.flags().trusted_dexes)
        row = self.store.add_trusted_dex(self.api, self.dex, by="admin:t", reason="test add")
        self.assertTrue(row["active"])
        self.assertIsNone(self.store.add_trusted_dex(self.api, self.dex, by="admin:t", reason="again"))
        self.assertIn(self.dex, repo.flags().trusted_dexes)
        gone = self.store.remove_trusted_dex(self.api, self.dex, by="admin:t", reason="test remove")
        self.assertFalse(gone["active"])
        self.assertIsNone(self.store.remove_trusted_dex(self.api, self.dex, by="admin:t", reason="again"))
        self.assertNotIn(self.dex, repo.flags().trusted_dexes)          # removal visible on the next tick
        again = self.store.add_trusted_dex(self.api, self.dex, by="admin:u", reason="re-add")
        self.assertTrue(again["active"])
        self.assertEqual(again["added_by"], "admin:u")
        self.store.remove_trusted_dex(self.api, self.dex, by="admin:t", reason="cleanup")

    def test_constraints_and_privileges(self) -> None:
        from app.db.engine import DbError

        with self.assertRaises(DbError):   # validator dex can never be removed (CHECK), even by raw SQL
            self.api.fetchall("""UPDATE trusted_dexes SET removed_at = now(), removed_by = 'x', removal_reason = 'x'
                                  WHERE dex = '' RETURNING dex""")
        with self.assertRaises(DbError):   # malformed name
            self.api.fetchall("INSERT INTO trusted_dexes (dex, added_by, reason) VALUES ('Bad-Dex', 'a', 'b')")
        with self.assertRaises(DbError):   # executor: read-only
            self.exe.fetchall("INSERT INTO trusted_dexes (dex, added_by, reason) VALUES ('zzexec', 'a', 'b')")
        with self.assertRaises(DbError):   # nobody deletes
            self.api.fetchall("DELETE FROM trusted_dexes WHERE dex = 'xyz' RETURNING dex")


if __name__ == "__main__":
    unittest.main()
