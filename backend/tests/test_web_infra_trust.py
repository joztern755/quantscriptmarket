"""REVIEW_WEB_INFRA fixes (backend side): executor agent attestation (H1), agent-substitution scan (H1), payout
wallet proofs (H1), CSP report sanitising (M2), executor selftest (M4) and migration 0013's privilege model.

Unit tests run everywhere (stdlib + cryptography). The DB part runs when AIJALON_TEST_DATABASE_URL points at a
scratch database migrated through 0013 and `psql` is on PATH (same harness as test_jobs_data_db.py):
    createdb -h localhost -p 55432 -U postgres aj_wi_test
    python3.12 backend/scripts/migrate.py --database-url postgresql://postgres@localhost:55432/aj_wi_test
    AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/aj_wi_test \\
        python3.12 -m unittest backend/tests/test_web_infra_trust.py
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from app.security import kms  # noqa: E402
from app.security.agent_keys import generate_sealed_agent_key  # noqa: E402

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
USER = "5b0f5f2e-3c5d-4d0e-9a51-2f1f7c0d9a11"
ADDR = "0x" + "ab" * 20


def _csp_module():
    """csp_report's pure sanitiser; without FastAPI installed, only the part above the route marker is loaded."""
    if importlib.util.find_spec("fastapi") is not None:
        from app.api.routers import csp_report
        return csp_report
    src = (HERE.parent / "app" / "api" / "routers" / "csp_report.py").read_text()
    pure = src.split("# ------------------------------------------------------------------------------ route (FastAPI)")[0]
    mod = SimpleNamespace()
    ns: dict = {"__name__": "csp_report_pure"}
    exec(compile(pure, "csp_report.py", "exec"), ns)  # noqa: S102 - our own source file
    mod.__dict__.update({k: v for k, v in ns.items() if not k.startswith("__")})
    return mod


class AttestationUnitTest(unittest.TestCase):
    def test_message_format(self) -> None:
        self.assertEqual(kms.agent_attestation_message(USER.upper(), ADDR.upper().replace("0X", "0x")),
                         f"aijalon-agent-v2|{USER}|{ADDR}".encode())
        for bad in (("not-a-uuid", ADDR), (USER, "0x12")):
            with self.assertRaises(ValueError):
                kms.agent_attestation_message(*bad)

    def test_local_signer_roundtrip_and_prod_refusal(self) -> None:
        s = kms.LocalAttestationSigner(is_prod=False)
        msg = kms.agent_attestation_message(USER, ADDR)
        sig = s.sign(msg)
        self.assertTrue(kms.verify_p256_signature(s.public_key_spki_der(), msg, sig))
        self.assertFalse(kms.verify_p256_signature(s.public_key_spki_der(), msg + b"x", sig))
        self.assertFalse(kms.verify_p256_signature(kms.LocalAttestationSigner(is_prod=False).public_key_spki_der(), msg, sig))
        self.assertFalse(kms.verify_p256_signature(b"garbage", msg, sig))
        with self.assertRaises(RuntimeError):
            kms.LocalAttestationSigner(is_prod=True)

    def test_factory_role_rules(self) -> None:
        with self.assertRaises(kms.Forbidden):
            kms.make_attestation_signer(SimpleNamespace(is_prod=True, service_role="api"))
        with self.assertRaises(RuntimeError):   # prod executor without a KMS key version
            kms.make_attestation_signer(SimpleNamespace(is_prod=True, service_role="executor", agent_attest_key_version=""))
        self.assertIsInstance(kms.make_attestation_signer(SimpleNamespace(is_prod=False, service_role="executor")),
                              kms.LocalAttestationSigner)

    def test_cloud_kms_signer_integrity_checks(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        name = "projects/p/locations/l/keyRings/r/cryptoKeys/agent-attest/cryptoKeyVersions/1"

        class FakeKms:
            def __init__(self, algorithm: int = 12, protection: int = 2, tamper: bool = False) -> None:
                self.algorithm, self.protection, self.tamper = algorithm, protection, tamper

            def get_public_key(self, request, timeout=None):
                return SimpleNamespace(pem=pem, pem_crc32c=kms.crc32c(pem.encode()), algorithm=self.algorithm,
                                       protection_level=self.protection, name=request["name"])

            def asymmetric_sign(self, request, timeout=None):
                from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
                from cryptography.hazmat.primitives import hashes
                assert request["digest_crc32c"] == kms.crc32c(request["digest"]["sha256"])
                sig = key.sign(request["digest"]["sha256"], ec.ECDSA(Prehashed(hashes.SHA256())))
                if self.tamper:
                    sig = key.sign(b"other", ec.ECDSA(hashes.SHA256()))
                return SimpleNamespace(signature=sig, signature_crc32c=kms.crc32c(sig), verified_digest_crc32c=True,
                                       name=request["name"])

        msg = kms.agent_attestation_message(USER, ADDR)
        s = kms.CloudKmsAttestationSigner(name, client=FakeKms())
        self.assertTrue(kms.verify_p256_signature(s.public_key_spki_der(), msg, s.sign(msg)))
        with self.assertRaises(kms.ExternalServiceError):
            kms.CloudKmsAttestationSigner(name, client=FakeKms(algorithm=31)).sign(msg)      # secp256k1 key refused
        with self.assertRaises(kms.ExternalServiceError):
            kms.CloudKmsAttestationSigner(name, client=FakeKms(protection=1)).sign(msg)      # software key refused
        with self.assertRaises(kms.ExternalServiceError):
            kms.CloudKmsAttestationSigner(name, client=FakeKms(tamper=True)).sign(msg)       # bad signature refused
        with self.assertRaises(ValueError):
            kms.CloudKmsAttestationSigner("projects/p/locations/l/keyRings/r/cryptoKeys/k", client=FakeKms())


class CspReportUnitTest(unittest.TestCase):
    def test_legacy_report_is_minimised(self) -> None:
        m = _csp_module()
        out = m.sanitize_reports({"csp-report": {
            "document-uri": "https://aijalon.trade/?token=SECRET#/dashboard", "violated-directive": "script-src-elem",
            "effective-directive": "script-src-elem", "blocked-uri": "https://evil.example/x.js?k=1",
            "script-sample": "alert(document.cookie)", "line-number": 12, "disposition": "enforce"}})
        self.assertEqual(out, [{"document": "https://aijalon.trade/", "directive": "script-src-elem",
                                "blocked": "https://evil.example/x.js", "source": "", "line": 12, "column": None,
                                "disposition": "enforce", "status": None}])
        self.assertNotIn("SECRET", json.dumps(out))
        self.assertNotIn("cookie", json.dumps(out))

    def test_reporting_api_list_and_limits(self) -> None:
        m = _csp_module()
        item = {"type": "csp-violation", "body": {"documentURL": "https://aijalon.trade/", "effectiveDirective": "require-trusted-types-for",
                                                  "blockedURL": "trusted-types-sink", "sample": "x" * 500}}
        out = m.sanitize_reports([item] * 20 + [{"type": "deprecation"}])
        self.assertEqual(len(out), m.MAX_REPORTS)
        self.assertEqual(out[0]["blocked"], "trusted-types-sink")
        self.assertEqual(m.sanitize_reports({"nope": 1}), [])
        self.assertEqual(m.sanitize_reports("x"), [])


# ============================================================================================== DB
try:
    import test_jobs_data_db as _harness  # noqa: E402
except Exception:  # noqa: BLE001
    _harness = None


@unittest.skipUnless(_harness is not None and _harness.RUN, "set AIJALON_TEST_DATABASE_URL (see module doc)")
class WebInfraDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        url = _harness.DB_URL
        cls.ex = _harness.RoleRunner(url, "app_executor")
        cls.api = _harness.RoleRunner(url, "app_api")
        cls.admin = _harness.RoleRunner(url, None)
        cls.db = _harness.RunnerDb(cls.ex)
        cls.tag = uuid.uuid4().hex[:8]
        cls.kek = b"k" * 32
        cls.enc = kms.EnvelopeEncryptor(kms.LocalAesKeyWrapper(cls.kek, is_prod=False))
        cls.dec = kms.EnvelopeDecryptor(kms.LocalAesKeyWrapper(cls.kek, is_prod=False, allow_unwrap=True))
        cls.signer = kms.LocalAttestationSigner(is_prod=False)

    def user(self) -> str:
        n = uuid.uuid4().hex[:10]
        return self.admin.fetchall("INSERT INTO users (firebase_uid, email) VALUES (:f, :e) RETURNING id::text AS id",
                                   {"f": f"fb{n}", "e": f"u{n}@x.io"})[0]["id"]

    def agent(self, uid: str, master: str, *, sealed_for: str | None = None, status: str = "pending_approval") -> tuple[str, str]:
        """Fixture row as the executor's keygen leaves it (keygen_at set; migrations/0016)."""
        sk = generate_sealed_agent_key(self.enc, user_id=sealed_for or uid)
        aid = self.admin.fetchall("""
            INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext, kms_key_version, status,
                                    keygen_at)
            VALUES (CAST(:u AS uuid), :m, :a, :c, :v, CAST(:s AS agent_key_status), now()) RETURNING id::text AS id""",
                                  {"u": uid, "m": master, "a": sk.address, "c": sk.ciphertext, "v": sk.key_version,
                                   "s": status})[0]["id"]
        return aid, sk.address

    def row(self, aid: str) -> dict:
        return self.admin.fetchall("""SELECT attestation_sig, attestation_key_version, attestation_failed_at,
                                             attestation_error FROM agent_keys WHERE id = CAST(:i AS uuid)""", {"i": aid})[0]

    def test_attest_agents_signs_verified_keys_and_flags_tampering(self) -> None:
        from app.execution.trust_jobs import attest_agents

        uid = self.user()
        good, good_addr = self.agent(uid, _harness._addr(int(self.tag, 16) + 1))
        # ciphertext sealed for ANOTHER user id: opening with this row's (user_id, address) AAD must fail
        bad, _ = self.agent(uid, _harness._addr(int(self.tag, 16) + 2), sealed_for=str(uuid.uuid4()))
        rep = attest_agents(self.db, T0, decryptor=self.dec, signer=self.signer, limit=1000)
        self.assertGreaterEqual(rep["attested"], 1)
        self.assertGreaterEqual(rep["failed"], 1)
        r = self.row(good)
        sig = base64.b64decode(r["attestation_sig"])
        self.assertTrue(kms.verify_p256_signature(self.signer.public_key_spki_der(),
                                                  kms.agent_attestation_message(uid, good_addr), sig))
        self.assertEqual(r["attestation_key_version"], self.signer.key_version)
        b = self.row(bad)
        self.assertIsNone(b["attestation_sig"])
        self.assertIsNotNone(b["attestation_failed_at"])
        alerts = self.admin.fetchall("SELECT severity::text AS s FROM events_outbox WHERE dedup_key = :d",
                                     {"d": f"agent_attestation_failed:{bad}"})
        self.assertEqual([a["s"] for a in alerts], ["critical"])
        # idempotent: nothing left to do for these rows
        again = attest_agents(self.db, T0, decryptor=self.dec, signer=self.signer, limit=1000)
        self.assertEqual(self.row(good)["attestation_sig"], r["attestation_sig"])
        self.assertEqual(again["failed"], 0)

    def test_privileges_and_immutability(self) -> None:
        from app.db.engine import DbError as AppDbError

        uid = self.user()
        master = _harness._addr(int(self.tag, 16) + 3)
        # the api may INSERT agent REQUESTS only: never key material, never an attestation (0013 + 0016)
        sk = generate_sealed_agent_key(self.enc, user_id=uid)
        with self.assertRaises((AppDbError, _harness.DbError)):
            self.api.fetchall("""INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext,
                                   kms_key_version, attestation_sig, attestation_key_version, attested_at)
                                 VALUES (CAST(:u AS uuid), :m, :a, :c, 'v', :s, 'k', now()) RETURNING id""",
                              {"u": uid, "m": master, "a": sk.address, "c": sk.ciphertext, "s": "A" * 88})
        aid, _ = self.agent(uid, master)
        # the api cannot write attestation columns at all
        with self.assertRaises((AppDbError, _harness.DbError)):
            self.api.fetchall("UPDATE agent_keys SET attestation_sig = :s, attestation_key_version = 'k', attested_at = now() "
                              "WHERE id = CAST(:i AS uuid) RETURNING id", {"s": "A" * 88, "i": aid})
        # the api can READ them (GET /v1/agents/{id}/attestation)
        self.api.fetchall("SELECT attestation_sig, attested_at FROM agent_keys WHERE id = CAST(:i AS uuid)", {"i": aid})
        # executor writes once; a second write is refused by the trigger
        self.ex.fetchall("UPDATE agent_keys SET attestation_sig = :s, attestation_key_version = 'k', attested_at = now() "
                         "WHERE id = CAST(:i AS uuid) RETURNING id", {"s": "A" * 88, "i": aid})
        with self.assertRaises((AppDbError, _harness.DbError)):
            self.ex.fetchall("UPDATE agent_keys SET attestation_sig = :s WHERE id = CAST(:i AS uuid) RETURNING id",
                             {"s": "B" * 88, "i": aid})
        # key material / identity immutable even for the executor's status updates path
        with self.assertRaises((AppDbError, _harness.DbError)):
            self.admin.fetchall("UPDATE agent_keys SET agent_address = :a WHERE id = CAST(:i AS uuid) RETURNING id",
                                {"a": _harness._addr(7), "i": aid})

    def test_wallet_proof_columns(self) -> None:
        from app.db.engine import DbError as AppDbError

        uid = self.user()
        addr = _harness._addr(int(self.tag, 16) + 4)
        self.api.fetchall("""INSERT INTO wallets (user_id, master_address, verified_at) VALUES (CAST(:u AS uuid), :a, now())
                             RETURNING id""", {"u": uid, "a": addr})
        # the SQL of app.api.routers.trust.record_wallet_proof, as the api role
        self.api.fetchall("""UPDATE wallets SET proof_message = :m, proof_signature = :s, proof_recorded_at = now()
                              WHERE user_id = CAST(:u AS uuid) AND lower(master_address) = lower(:a) RETURNING id""",
                          {"m": "x" * 60, "s": "0x" + "1" * 130, "u": uid, "a": addr})
        with self.assertRaises((AppDbError, _harness.DbError)):
            self.api.fetchall("UPDATE wallets SET proof_signature = 'nope' WHERE user_id = CAST(:u AS uuid) RETURNING id",
                              {"u": uid})

    def test_agent_substitution_scan(self) -> None:
        from app.execution.trust_jobs import agent_substitution_scan

        uid = self.user()
        master = _harness._addr(int(self.tag, 16) + 5)
        _, ours = self.agent(uid, master, status="active")
        foreign = _harness._addr(int(self.tag, 16) + 6)
        info = _harness.FakeInfo()
        info.agents[master] = [{"name": "aijalon", "address": ours, "validUntil": 0},
                               {"name": "aijalon valid_until 1790000000000", "address": foreign, "validUntil": 0},
                               {"name": "other-bot", "address": _harness._addr(int(self.tag, 16) + 7)}]
        settings = SimpleNamespace(agent_name="aijalon", hl_limits=None)
        rep = agent_substitution_scan(self.db, T0, info=info, settings=settings, rate_budget=None)
        self.assertGreaterEqual(rep["substituted"], 1)
        ops = self.admin.fetchall("SELECT severity::text AS s, payload FROM events_outbox WHERE dedup_key = :d",
                                  {"d": f"agent_substituted:{master}:{foreign}"})
        self.assertEqual(ops[0]["s"], "critical")
        self.assertNotIn(foreign, json.dumps(ops[0]["payload"]))   # short addresses only
        usr = self.admin.fetchall("SELECT kind, user_id::text AS u FROM events_outbox WHERE dedup_key = :d",
                                  {"d": f"agent_substituted_user:{master}:{foreign}"})
        self.assertEqual((usr[0]["kind"], usr[0]["u"]), ("agent_revoked", uid))
        # re-run: deduplicated, our own agent never flagged
        agent_substitution_scan(self.db, T0, info=info, settings=settings, rate_budget=None)
        self.assertEqual(len(self.admin.fetchall("SELECT 1 AS x FROM events_outbox WHERE dedup_key = :d",
                                                 {"d": f"agent_substituted:{master}:{foreign}"})), 1)
        self.assertEqual(self.admin.fetchall("SELECT count(*) AS n FROM events_outbox WHERE dedup_key = :d",
                                             {"d": f"agent_substituted:{master}:{ours}"})[0]["n"], 0)

    def test_executor_selftest_is_read_only(self) -> None:
        from contextlib import contextmanager

        from app.execution.trust_jobs import executor_selftest

        class FakeRuntime:
            settings = SimpleNamespace(is_prod=False, service_role="executor")
            info = SimpleNamespace(meta=lambda: {"universe": [{"name": "BTC"}]})
            opened: list = []

            def executor(self, db, now=None):
                return object()

            def key_provider(self, db):
                rt = self

                class KP:
                    @contextmanager
                    def agent_key(self, user_id, master):
                        rt.opened.append((user_id, master))
                        yield bytearray(32)
                return KP()

        before = self.admin.fetchall("SELECT (SELECT count(*) FROM audit_log) AS a, (SELECT count(*) FROM events_outbox) AS e")[0]
        rt = FakeRuntime()
        out = executor_selftest(self.db, T0, runtime=rt, signer_factory=lambda: self.signer)
        after = self.admin.fetchall("SELECT (SELECT count(*) FROM audit_log) AS a, (SELECT count(*) FROM events_outbox) AS e")[0]
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["no_orders"])
        self.assertEqual(before, after)                    # nothing written
        self.assertIn("app_executor", out["checks"]["db"])
        bad = executor_selftest(self.db, T0, runtime=rt, signer_factory=lambda: (_ for _ in ()).throw(RuntimeError("x")))
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["checks"]["attestation_key"].startswith("FAIL"))


if __name__ == "__main__":
    unittest.main()
