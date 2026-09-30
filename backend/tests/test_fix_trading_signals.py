"""REVIEW_TRADING_KEYS F4 (mandatory in-house script-hash pin) + F1 at signal ingest (trusted dexes).

Reproduce → blocked:
* verify_and_parse without a pin used to accept any validly signed engine; with ``require_script_pin=True`` (what the
  ingest job passes) a listed strategy without a pin is rejected;
* DB: an in-house strategy version cannot be stored without ``params.script_sha256`` (trigger, migration 0012);
  creator versions are unaffected; if an unpinned in-house version exists anyway (trigger bypassed), ingest refuses it
  (``signals_unpinned`` critical + markets paused) and stores nothing for it;
* a record for a coin on a non-trusted dex is not stored (``signals_untrusted_dex`` + auto-pause); the allowlist
  loader fails closed (None) when the table cannot be read.
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from app.config import RiskLimits  # noqa: E402
from app.strategies import signals as sig  # noqa: E402

DB_URL = os.environ.get("AIJALON_TEST_DATABASE_URL", "")


def _psql_ok() -> bool:
    try:
        return subprocess.run(["psql", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


RUN_DB = bool(DB_URL) and _psql_ok()
SCRIPT = "e" * 64


def feed(now: datetime, script: str = SCRIPT, weight: int = 1) -> tuple[bytes, bytes, str]:
    key = Ed25519PrivateKey.generate()
    pub = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    as_of = (now - timedelta(days=1)).date()
    obj = {"as_of": as_of.isoformat(),
           "engine_sha256": hashlib.sha256(sig.canonical_json({"silver": script})).hexdigest(),
           "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "strategies": {"silver": {"last_action": "BUY" if weight else "SELL", "last_action_date": as_of.isoformat(),
                                     "market": "xyz:SILVER", "script_sha256": script, "status": "trades",
                                     "target_weight": weight}}}
    body = sig.canonical_json(obj)
    return body, base64.b64encode(key.sign(body)), pub


class Resp:
    def __init__(self, data: bytes) -> None:
        self.status_code, self.headers, self._d = 200, {}, data

    def iter_content(self, chunk_size: int = 0):
        yield self._d

    def close(self) -> None:
        pass


class Session:
    def __init__(self, b: bytes, s: bytes) -> None:
        self.b, self.s = b, s

    def get(self, url: str, **_: Any) -> Resp:
        return Resp(self.s if url.endswith(".sig") else self.b)


class PinRequiredUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.body, self.sig, self.pub = feed(self.now)

    def test_reproduction_without_pin_any_engine_accepted(self) -> None:
        batch = sig.verify_and_parse(self.body, self.sig, pubkey_b64=self.pub, now=self.now, listed_keys=("silver",))
        self.assertEqual(batch.records[0].script_sha256, SCRIPT)          # old behaviour (pin optional)

    def test_pin_required_rejects_unpinned(self) -> None:
        with self.assertRaises(sig.SignalEngineMismatch):
            sig.verify_and_parse(self.body, self.sig, pubkey_b64=self.pub, now=self.now, listed_keys=("silver",),
                                 require_script_pin=True)
        with self.assertRaises(sig.SignalEngineMismatch):
            sig.verify_and_parse(self.body, self.sig, pubkey_b64=self.pub, now=self.now, listed_keys=("silver",),
                                 expected_script_sha256={"silver": "f" * 64}, require_script_pin=True)
        ok = sig.verify_and_parse(self.body, self.sig, pubkey_b64=self.pub, now=self.now, listed_keys=("silver",),
                                  expected_script_sha256={"silver": SCRIPT}, require_script_pin=True)
        self.assertEqual(len(ok.records), 1)

    def test_trusted_loader_fails_closed(self) -> None:
        from app.jobs_data import signals as job

        class Broken:
            def fetchall(self, sql, params=None):
                raise RuntimeError("no table")
        self.assertIsNone(job._trusted_dexes(Broken()))

    def test_ingest_always_requires_pin(self) -> None:
        src = (Path(__file__).resolve().parents[1] / "app/jobs_data/signals.py").read_text()
        self.assertIn('"require_script_pin": True', src)


if RUN_DB:
    from test_jobs_data_db import RoleRunner  # noqa: E402


@unittest.skipUnless(RUN_DB, "needs AIJALON_TEST_DATABASE_URL (migrated through 0012) and psql")
class SignalsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.admin = RoleRunner(DB_URL, None)
        cls.db = RoleRunner(DB_URL, "app_executor")

    def _strategy(self, *, in_house: bool, market: str) -> str:
        slug = "t" + uuid.uuid4().hex[:10]
        owner = None
        if not in_house:
            owner = self.admin.fetchall("""INSERT INTO users (firebase_uid, email, referral_code, mfa_enrolled)
                                           VALUES (:u, :e, :c, true) RETURNING id::text AS id""",
                                        {"u": "fb" + slug, "e": f"{slug}@example.test", "c": "R" + slug})[0]["id"]
        return self.admin.fetchall("""INSERT INTO strategies (slug, name, in_house, owner_user_id, markets, timeframe,
                                             price_monthly_micro, profit_share_bps, status)
                                      VALUES (:s, :s, :ih, CAST(:o AS uuid), ARRAY[:m], '1d', 0, 0, 'listed')
                                      RETURNING id::text AS id, slug""",
                                   {"s": slug, "ih": in_house, "o": owner, "m": market})[0]

    def _version(self, sid: str, params: str) -> list:
        return self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, params)
                                      VALUES (CAST(:s AS uuid), 1, 'terminal', CAST(:p AS jsonb))
                                      RETURNING id::text AS id""", {"s": sid, "p": params})

    def test_trigger_requires_pin_for_in_house_only(self) -> None:
        from app.db.engine import DbError

        ih = self._strategy(in_house=True, market="xyz:SILVER")
        with self.assertRaises(DbError) as cm:
            self._version(ih["id"], "{}")
        self.assertEqual(cm.exception.sqlstate, "23514")
        with self.assertRaises(DbError):
            self._version(ih["id"], '{"script_sha256": "NOT-HEX"}')
        self.assertTrue(self._version(ih["id"], '{"script_sha256": "%s"}' % ("a" * 64)))
        with self.assertRaises(DbError):   # a later UPDATE cannot drop the pin either
            self.admin.fetchall("""UPDATE strategy_versions SET params = '{}'::jsonb
                                    WHERE strategy_id = CAST(:s AS uuid) RETURNING id""", {"s": ih["id"]})
        creator = self._strategy(in_house=False, market="xyz:SILVER")
        self.assertTrue(self._version(creator["id"], "{}"))

    def _ingest(self, now, keys, body, sigb, pub):
        from app.strategies.signals import ingest as signals_ingest

        settings = SimpleNamespace(signals_pubkey_b64=pub, signals_url="https://t.example/signals.json",
                                   in_house_listed=tuple(keys), risk=RiskLimits())
        return signals_ingest(db=self.db, now=now, settings=settings, session=Session(body, sigb))

    def test_unpinned_in_house_version_is_refused(self) -> None:
        coin = "xyz:ZZ" + uuid.uuid4().hex[:6].upper()
        st = self._strategy(in_house=True, market=coin)
        self.admin.fetchall("ALTER TABLE strategy_versions DISABLE TRIGGER strategy_versions_inhouse_pin")
        try:
            self.admin.fetchall("""INSERT INTO strategy_versions (strategy_id, version, code_hash, params, markets)
                                   VALUES (CAST(:s AS uuid), 1, 'terminal', '{}'::jsonb, ARRAY[:m])""",
                                {"s": st["id"], "m": coin})
        finally:
            self.admin.fetchall("ALTER TABLE strategy_versions ENABLE TRIGGER strategy_versions_inhouse_pin")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        body, sigb, pub = feed(now)
        out = self._ingest(now, [st["slug"]], body, sigb, pub)
        self.assertFalse(out["ok"])
        self.assertEqual(out["unpinned"], [st["slug"]])
        self.assertIn(coin, out.get("paused_markets", []))
        self.assertTrue(self.admin.fetchall("""SELECT 1 FROM events_outbox WHERE kind = 'signals_unpinned'
                                                AND severity = 'critical' AND payload->>'strategy_key' = :k""",
                                            {"k": st["slug"]}))
        self.admin.fetchall("UPDATE system_flags SET value = 'false', updated_by = 'test' WHERE key = :k",
                            {"k": f"new_entries_paused:{coin}"})

    def test_untrusted_dex_record_not_stored(self) -> None:
        from app.jobs_data import signals as job

        silver = self.admin.fetchall("""SELECT v.id::text AS vid, v.params->>'script_sha256' AS pin
                                          FROM strategy_versions v JOIN strategies s ON s.id = v.strategy_id
                                         WHERE s.slug = 'silver' ORDER BY v.version DESC LIMIT 1""")[0]
        now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)   # a bar nobody stored yet
        body, sigb, pub = feed(now, script=silver["pin"], weight=2)
        before = self.admin.fetchall("SELECT count(*) AS n FROM signals WHERE strategy_version_id = CAST(:v AS uuid)",
                                     {"v": silver["vid"]})[0]["n"]
        try:
            with mock.patch.object(job, "_trusted_dexes", return_value=frozenset({""})):   # xyz removed
                out = self._ingest(now, ["silver"], body, sigb, pub)
            self.assertTrue(out["ok"], out)
            self.assertEqual((out["stored"], out.get("untrusted")), (0, 1))
            after = self.admin.fetchall("""SELECT count(*) AS n FROM signals
                                            WHERE strategy_version_id = CAST(:v AS uuid)""", {"v": silver["vid"]})
            self.assertEqual(after[0]["n"], before)
            self.assertTrue(self.admin.fetchall("""SELECT 1 FROM events_outbox WHERE kind = 'signals_untrusted_dex'
                                                    AND payload->>'coin' = 'xyz:SILVER'"""))
            flag = self.admin.fetchall("""SELECT value::text AS v FROM system_flags
                                           WHERE key = 'new_entries_paused:xyz:SILVER'""")
            self.assertEqual(flag, [{"v": "true"}])
        finally:
            self.admin.fetchall("""UPDATE system_flags SET value = 'false', updated_by = 'test'
                                    WHERE key = 'new_entries_paused:xyz:SILVER'""")


if __name__ == "__main__":
    unittest.main()
