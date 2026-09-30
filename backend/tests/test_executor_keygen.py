"""Executor-side agent key generation (migrations/0016; docs/security/REVIEW_WEB_INFRA.md H1 residual).

The api process must have NO code path that can generate or seal an agent key (a compromised api could otherwise
plant a key it knows and get it attested), and the api DB role must not be able to write agent key material. The
executor generates the key, seals it, proves the sealed blob re-opens to the address, attests it and writes it once.

Unit tests run everywhere (stdlib + cryptography). The DB part runs when AIJALON_TEST_DATABASE_URL points at a scratch
database migrated through 0016 and `psql` is on PATH (same harness as test_jobs_data_db.py / test_web_infra_trust.py):
    createdb -h localhost -p 55432 -U postgres aj_kg_test
    python3.12 backend/scripts/migrate.py --database-url postgresql://postgres@localhost:55432/aj_kg_test
    AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/aj_kg_test \\
        python3.12 -m unittest backend/tests/test_executor_keygen.py
"""
from __future__ import annotations

import ast
import base64
import re
import subprocess
import sys
import threading
import unittest
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

from app.errors import ExternalServiceError, Forbidden  # noqa: E402
from app.security import kms  # noqa: E402
from app.security.agent_keys import SealedKey, address_from_private_key, open_agent_key  # noqa: E402

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
KEK = b"k" * 32
FORBIDDEN_API_NAMES = frozenset({
    "generate_sealed_agent_key", "seal_agent_key", "generate_agent_key", "make_encryptor", "EnvelopeEncryptor",
    "open_agent_key", "opened_agent_key", "make_decryptor", "EnvelopeDecryptor", "LocalAesKeyWrapper",
    "CloudKmsKeyWrapper", "agent_key_aad",
})


def _enc_dec() -> tuple[Any, Any]:
    return (kms.EnvelopeEncryptor(kms.LocalAesKeyWrapper(KEK, is_prod=False)),
            kms.EnvelopeDecryptor(kms.LocalAesKeyWrapper(KEK, is_prod=False, allow_unwrap=True)))


# ================================================================================================ the api has no path
class ApiHasNoKeygenPathTest(unittest.TestCase):
    def _api_sources(self) -> list[Path]:
        files = sorted((BACKEND / "app" / "api").rglob("*.py"))
        self.assertGreater(len(files), 10)
        return files

    def test_no_api_module_references_agent_key_generation_or_sealing(self) -> None:
        """AST scan of app/api/**: no call, import, attribute or string lookup (getattr/_require) of any agent-key
        generation / sealing / opening primitive, and no import of app.security.agent_keys at all."""
        hits: list[str] = []
        for f in self._api_sources():
            tree = ast.parse(f.read_text(), str(f))
            for node in ast.walk(tree):
                where = f"{f.relative_to(BACKEND)}:{getattr(node, 'lineno', '?')}"
                if isinstance(node, ast.Name) and node.id in FORBIDDEN_API_NAMES:
                    hits.append(f"{where} name {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_API_NAMES:
                    hits.append(f"{where} attr {node.attr}")
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                        node.value in FORBIDDEN_API_NAMES or node.value == "app.security.agent_keys"):
                    hits.append(f"{where} string {node.value!r}")
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.security.agent_keys"):
                    hits.append(f"{where} import from {node.module}")
                elif isinstance(node, ast.ImportFrom) and node.module == "app.security.kms":
                    bad = [a.name for a in node.names if a.name in FORBIDDEN_API_NAMES]
                    if bad:
                        hits.append(f"{where} import {bad}")
        self.assertEqual(hits, [], "the api must not be able to generate / seal / open agent keys (migrations/0016)")

    def test_no_api_sql_writes_key_material(self) -> None:
        """No INSERT/UPDATE statement in app/api/** names agent_keys key-material columns."""
        pat = re.compile(r"(INSERT\s+INTO\s+agent_keys[^;]*?\)|UPDATE\s+agent_keys\s+SET[^;]*?WHERE)", re.I | re.S)
        for f in self._api_sources():
            for m in pat.finditer(f.read_text()):
                stmt = m.group(0)
                for col in ("agent_address", "key_ciphertext", "kms_key_version", "keygen_at"):
                    self.assertNotRegex(stmt, rf"\b{col}\b", f"{f.name}: {stmt[:120]}")

    def test_services_have_no_agent_key_port(self) -> None:
        src = (BACKEND / "app" / "api" / "deps.py").read_text()
        self.assertNotIn("agent_keys:", src)
        self.assertNotIn("class AgentKeyPort", src)
        self.assertNotIn("SealedAgentKey", src)
        self.assertNotIn("AgentKeyAdapter", (BACKEND / "app" / "api" / "adapters.py").read_text())

    def test_agent_key_encryptor_refuses_api_role(self) -> None:
        base = dict(env="test", is_prod=False, kms_key_name="", local_dev_kek_b64=base64.b64encode(KEK).decode())
        for role in ("api", "sandbox", "", None):
            with self.assertRaises(Forbidden):
                kms.make_encryptor(SimpleNamespace(**base, service_role=role))
        with self.assertRaises(Forbidden):   # "all" (single-process dev) is refused in prod
            kms.make_encryptor(SimpleNamespace(env="prod", is_prod=True, service_role="all",
                                               kms_key_name="projects/p/locations/l/keyRings/r/cryptoKeys/agent-keys",
                                               local_dev_kek_b64=""), kms_client=object())
        self.assertIsInstance(kms.make_encryptor(SimpleNamespace(**base, service_role="executor")), kms.EnvelopeEncryptor)
        # the api keeps sealing creator code (dedicated key)
        self.assertIsInstance(kms.make_code_encryptor(SimpleNamespace(**base, service_role="api",
                                                                      creator_code_kms_key_name="")),
                              kms.EnvelopeEncryptor)

    def test_attestation_is_v2(self) -> None:
        uid = str(uuid.uuid4())
        self.assertTrue(kms.agent_attestation_message(uid, "0x" + "ab" * 20).startswith(b"aijalon-agent-v2|"))
        self.assertIn('AGENT_ATTEST_PREFIX = "aijalon-agent-v2"', (ROOT / "web/src/core/attest.ts").read_text())


class MigrationTextTest(unittest.TestCase):
    def test_0016_privileges(self) -> None:
        sql = (BACKEND / "migrations" / "0016_executor_keygen.sql").read_text()
        self.assertIn("REVOKE INSERT ON agent_keys FROM app_api;", sql)
        grant = re.search(r"GRANT INSERT \(([^)]*)\) ON agent_keys TO app_api;", sql)
        self.assertIsNotNone(grant)
        cols = {c.strip() for c in grant.group(1).split(",")}
        self.assertEqual(cols, {"user_id", "master_address", "agent_name", "status"})
        self.assertIn("GRANT UPDATE (agent_address, key_ciphertext, kms_key_version, keygen_at) ON agent_keys TO app_executor;",
                      sql)
        self.assertNotRegex(sql, r"GRANT INSERT[^;]*agent_keys TO app_executor")

    def test_infra_iam_and_verify(self) -> None:
        boot = (ROOT / "infra/gcp/bootstrap.sh").read_text()
        self.assertRegex(boot, r'remove-iam-policy-binding "\$\{KMS_KEY\}"[^\n]*\\\n\s*--member="serviceAccount:\$\{SA_API\}"')
        verify = (ROOT / "infra/gcp/sql/20_verify.sql").read_text()
        self.assertIn("api can write agent_keys.%s", verify)
        mon = (ROOT / "infra/gcp/monitoring.py").read_text()
        self.assertIn("SEC: agent-keys ENCRYPT by anyone but executor", mon)


# ================================================================================ executor keygen (fake DB, no SQL)
class _FakeKeyDb:
    """Interprets exactly the statements of trust_jobs.generate_agents / ops alerts (enough to unit-test the logic)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {r["id"]: dict(r) for r in rows}
        self.events: list[dict[str, Any]] = []
        self.on_update: Any = None

    def begin(self) -> Any:
        return nullcontext(self)

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        p = dict(params or {})
        s = " ".join(sql.split())
        if s.startswith("SELECT id::text AS id, user_id::text AS user_id, master_address FROM agent_keys"):
            todo = [r for r in self.rows.values() if r["status"] == "requested" and r.get("key_ciphertext") is None
                    and r.get("attestation_failed_at") is None]
            return [{"id": r["id"], "user_id": r["user_id"], "master_address": r["master_address"]}
                    for r in todo][: int(p["n"])]
        if s.startswith("UPDATE agent_keys SET agent_address"):
            if self.on_update:
                self.on_update(p)
            r = self.rows.get(p["id"])
            if r is None or r["status"] != "requested" or r.get("key_ciphertext") is not None:
                return []
            r.update(agent_address=p["a"], key_ciphertext=p["c"], kms_key_version=p["v"], keygen_at=p["t"],
                     status="pending_approval", attestation_sig=p["s"], attestation_key_version=p["kv"],
                     attested_at=p["t"] if p["s"] is not None else None)
            return [{"id": r["id"]}]
        if s.startswith("INSERT INTO events_outbox"):
            self.events.append(p)
            return [{"id": "e"}]
        raise AssertionError(f"unexpected SQL: {s[:120]}")


class GenerateAgentsUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.uid = str(uuid.uuid4())
        self.aid = str(uuid.uuid4())
        self.db = _FakeKeyDb([{"id": self.aid, "user_id": self.uid, "master_address": "0x" + "11" * 20,
                               "status": "requested", "key_ciphertext": None}])
        self.enc, self.dec = _enc_dec()
        self.signer = kms.LocalAttestationSigner(is_prod=False)

    def test_generates_seals_reopens_and_attests(self) -> None:
        from app.execution.trust_jobs import generate_agents

        rep = generate_agents(self.db, T0, encryptor=self.enc, decryptor=self.dec, signer=self.signer)
        self.assertEqual((rep["generated"], rep["attested"], rep["errors"]), (1, 1, []))
        r = self.db.rows[self.aid]
        self.assertEqual(r["status"], "pending_approval")
        self.assertRegex(r["agent_address"], r"^0x[0-9a-f]{40}$")
        # the stored blob opens ONLY for this (user, address) and derives the stored address
        priv = open_agent_key(SealedKey(r["key_ciphertext"], r["kms_key_version"], r["agent_address"]), self.dec,
                              user_id=self.uid)
        self.assertEqual(address_from_private_key(priv), r["agent_address"])
        kms.zeroize(priv)
        with self.assertRaises(kms.DecryptionFailed):
            open_agent_key(SealedKey(r["key_ciphertext"], r["kms_key_version"], r["agent_address"]), self.dec,
                           user_id=str(uuid.uuid4()))
        # v2 attestation by the executor's signer over exactly (user, address)
        self.assertTrue(kms.verify_p256_signature(self.signer.public_key_spki_der(),
                                                  kms.agent_attestation_message(self.uid, r["agent_address"]),
                                                  base64.b64decode(r["attestation_sig"])))
        self.assertEqual(r["attestation_key_version"], self.signer.key_version)
        # nothing secret in the report
        self.assertNotIn("ciphertext", repr(rep))
        again = generate_agents(self.db, T0, encryptor=self.enc, decryptor=self.dec, signer=self.signer)
        self.assertEqual((again["todo"], again["generated"]), (0, 0))

    def test_withdrawn_request_discards_the_key(self) -> None:
        from app.execution.trust_jobs import generate_agents

        def revoke(_p: dict) -> None:       # POST /agents rotation / a newer request, between SELECT and UPDATE
            self.db.rows[self.aid]["status"] = "revoked"
        self.db.on_update = revoke
        rep = generate_agents(self.db, T0, encryptor=self.enc, decryptor=self.dec, signer=self.signer)
        self.assertEqual((rep["generated"], rep["withdrawn"]), (0, 1))
        self.assertIsNone(self.db.rows[self.aid]["key_ciphertext"])

    def test_signer_outage_stores_key_without_attestation(self) -> None:
        from app.execution.trust_jobs import generate_agents

        class Down:
            key_version = "x"

            def sign(self, m: bytes) -> bytes:
                raise ExternalServiceError("kms down", service="kms")
        rep = generate_agents(self.db, T0, encryptor=self.enc, decryptor=self.dec, signer=Down())
        r = self.db.rows[self.aid]
        self.assertEqual((rep["generated"], rep["attested"]), (1, 0))
        self.assertIsNone(r["attestation_sig"])
        self.assertEqual(r["status"], "pending_approval")   # attest_agents retries the signature next minute

    def test_blob_that_does_not_reopen_pages_ops_and_stores_nothing(self) -> None:
        from app.execution.trust_jobs import generate_agents

        other_dec = kms.EnvelopeDecryptor(kms.LocalAesKeyWrapper(b"z" * 32, is_prod=False, allow_unwrap=True))
        rep = generate_agents(self.db, T0, encryptor=self.enc, decryptor=other_dec, signer=self.signer)
        self.assertEqual(rep["generated"], 0)
        self.assertIsNone(self.db.rows[self.aid]["key_ciphertext"])
        self.assertEqual([e["k"] for e in self.db.events], ["agent_keygen_failed"])
        self.assertEqual(self.db.events[0]["sev"], "critical")

    def test_kms_encrypt_failure_stops_the_run(self) -> None:
        from app.execution.trust_jobs import generate_agents

        class Broken:
            def seal(self, *_a: Any, **_k: Any) -> Any:
                raise ExternalServiceError("kms encrypt failed", service="kms")
        rep = generate_agents(self.db, T0, encryptor=Broken(), decryptor=self.dec, signer=self.signer)
        self.assertEqual((rep["generated"], rep["errors"]), (0, ["seal:ExternalServiceError"]))


# ============================================================================================================ DB
try:
    import test_jobs_data_db as _harness  # noqa: E402
except Exception:  # noqa: BLE001
    _harness = None


def _db_errors() -> tuple[type, ...]:
    from app.db.engine import DbError as AppDbError
    return (AppDbError, _harness.DbError)


@unittest.skipUnless(_harness is not None and _harness.RUN, "set AIJALON_TEST_DATABASE_URL (see module doc)")
class ExecutorKeygenDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        url = _harness.DB_URL
        cls.url = url
        cls.ex = _harness.RoleRunner(url, "app_executor")
        cls.api = _harness.RoleRunner(url, "app_api")
        cls.admin = _harness.RoleRunner(url, None)
        cls.db = _harness.RunnerDb(cls.ex)
        cls.enc, cls.dec = _enc_dec()
        cls.signer = kms.LocalAttestationSigner(is_prod=False)
        cls.seed = int(uuid.uuid4().hex[:8], 16)
        cls.n = 0

    def master(self) -> str:
        type(self).n += 1
        return _harness._addr(self.seed * 100 + self.n)

    def user(self) -> str:
        n = uuid.uuid4().hex[:10]
        return self.admin.fetchall("INSERT INTO users (firebase_uid, email) VALUES (:f, :e) RETURNING id::text AS id",
                                   {"f": f"fb{n}", "e": f"u{n}@x.io"})[0]["id"]

    def request(self, uid: str, master: str) -> str:
        """Exactly app.api.store.SqlStore.insert_agent_request, as the api role."""
        return self.api.fetchall("""
            INSERT INTO agent_keys (user_id, master_address, agent_name, status)
            VALUES (CAST(:u AS uuid), :m, 'aijalon', CAST('requested' AS agent_key_status))
            RETURNING id::text AS id, agent_address, status::text AS status""", {"u": uid, "m": master})[0]["id"]

    def row(self, aid: str) -> dict:
        return self.admin.fetchall("""
            SELECT user_id::text AS user_id, master_address, agent_address, key_ciphertext, kms_key_version,
                   keygen_at, status::text AS status, attestation_sig, attestation_key_version, attested_at
              FROM agent_keys WHERE id = CAST(:i AS uuid)""", {"i": aid})[0]

    def generate(self, **kw: Any) -> dict:
        from app.execution.trust_jobs import generate_agents
        return generate_agents(self.db, T0, encryptor=self.enc, decryptor=self.dec, signer=self.signer,
                               limit=1000, **kw)

    # ------------------------------------------------------------------------------------------ api privileges
    def test_api_role_cannot_create_or_write_key_material(self) -> None:
        errs = _db_errors()
        uid, master = self.user(), self.master()
        from app.security.agent_keys import generate_sealed_agent_key
        sk = generate_sealed_agent_key(self.enc, user_id=uid)
        # INSERT naming any key-material column: insufficient privilege (42501), whatever the status
        for cols, vals, prm in (
                ("agent_address, key_ciphertext, kms_key_version", ":a, :c, 'v'", {"a": sk.address, "c": sk.ciphertext}),
                ("agent_address", ":a", {"a": sk.address}),
                ("key_ciphertext", ":c", {"c": sk.ciphertext}),
                ("kms_key_version", "'v'", {}),
                ("keygen_at", "now()", {})):
            with self.assertRaises(errs) as cm:
                self.api.fetchall(f"""INSERT INTO agent_keys (user_id, master_address, agent_name, status, {cols})
                                      VALUES (CAST(:u AS uuid), :m, 'aijalon', 'pending_approval', {vals})
                                      RETURNING id""", {"u": uid, "m": master, **prm})
            self.assertEqual(getattr(cm.exception, "sqlstate", None), "42501", cols)
        # a key-less row that is not a request is refused (trigger / CHECK)
        with self.assertRaises(errs):
            self.api.fetchall("""INSERT INTO agent_keys (user_id, master_address, agent_name, status)
                                 VALUES (CAST(:u AS uuid), :m, 'aijalon', 'pending_approval') RETURNING id""",
                              {"u": uid, "m": master})
        aid = self.request(uid, master)
        self.assertIsNone(self.row(aid)["agent_address"])
        # the api cannot fill a request's key material, nor move it forward without one
        for col, val in (("agent_address", f"'{sk.address}'"), ("key_ciphertext", "'\\x00'::bytea"),
                         ("kms_key_version", "'v'"), ("keygen_at", "now()")):
            with self.assertRaises(errs) as cm:
                self.api.fetchall(f"UPDATE agent_keys SET {col} = {val} WHERE id = CAST(:i AS uuid) RETURNING id",
                                  {"i": aid})
            self.assertEqual(getattr(cm.exception, "sqlstate", None), "42501", col)
        for status in ("pending_approval", "active"):
            with self.assertRaises(errs):
                self.api.fetchall("UPDATE agent_keys SET status = CAST(:s AS agent_key_status) "
                                  "WHERE id = CAST(:i AS uuid) RETURNING id", {"s": status, "i": aid})
        # the api CAN withdraw its own request (rotation / newer request) and can read the non-secret columns
        self.api.fetchall("SELECT id, agent_address, keygen_at, attestation_sig FROM agent_keys WHERE id = CAST(:i AS uuid)",
                          {"i": aid})
        with self.assertRaises(errs):   # still never the ciphertext
            self.api.fetchall("SELECT key_ciphertext FROM agent_keys WHERE id = CAST(:i AS uuid)", {"i": aid})
        self.api.fetchall("UPDATE agent_keys SET status = 'revoked', revoked_at = now() WHERE id = CAST(:i AS uuid) "
                          "RETURNING id", {"i": aid})
        # the executor has no INSERT on agent_keys at all
        with self.assertRaises(errs):
            self.ex.fetchall("""INSERT INTO agent_keys (user_id, master_address, agent_name, status)
                                VALUES (CAST(:u AS uuid), :m, 'aijalon', 'requested') RETURNING id""",
                             {"u": uid, "m": self.master()})

    # ------------------------------------------------------------------------------------------ end to end
    def test_executor_generates_attests_and_the_tick_can_open_it(self) -> None:
        from app.execution.keys import AgentKeyMissing, DbAgentKeyProvider
        from app.execution.pg import PgDatabase, as_bytes

        uid, master = self.user(), self.master()
        aid = self.request(uid, master)
        rep = self.generate()
        self.assertGreaterEqual(rep["generated"], 1, rep)
        r = self.row(aid)
        self.assertEqual(r["status"], "pending_approval")
        self.assertIsNotNone(r["keygen_at"])
        addr = r["agent_address"]
        self.assertTrue(kms.verify_p256_signature(self.signer.public_key_spki_der(),
                                                  kms.agent_attestation_message(uid, addr),
                                                  base64.b64decode(r["attestation_sig"])))
        # the api's GET /v1/agents/{id} query (store.get_agent_detail), as the api role
        det = self.api.fetchall("""SELECT agent_address, status::text AS status, keygen_at, attestation_sig,
                                          attestation_key_version, attested_at, attestation_failed_at
                                     FROM agent_keys WHERE id = CAST(:i AS uuid) AND user_id = CAST(:u AS uuid)""",
                                {"i": aid, "u": uid})[0]
        self.assertEqual((det["agent_address"], det["attestation_sig"]), (addr, r["attestation_sig"]))
        provider = DbAgentKeyProvider(PgDatabase(self.db), decryptor_factory=lambda: self.dec)
        with self.assertRaises(AgentKeyMissing):       # not active yet: the tick cannot use it
            with provider.agent_key(uid, master):
                pass
        # POST /agents/{id}/confirm (api role) activates the executor-generated key
        self.api.fetchall("UPDATE agent_keys SET status = 'active', approved_at = now() WHERE id = CAST(:i AS uuid) "
                          "RETURNING id", {"i": aid})
        with provider.agent_key(uid, master) as priv:
            self.assertEqual(address_from_private_key(priv), addr)
        # the executor's write is once-only; nobody rewrites key material
        for runner in (self.ex, self.admin):
            with self.assertRaises(_db_errors()):
                runner.fetchall("UPDATE agent_keys SET key_ciphertext = :c WHERE id = CAST(:i AS uuid) RETURNING id",
                                {"c": as_bytes(r["key_ciphertext"]) + b"\x00", "i": aid})
        with self.assertRaises(_db_errors()):
            self.ex.fetchall("UPDATE agent_keys SET agent_address = :a WHERE id = CAST(:i AS uuid) RETURNING id",
                             {"a": self.master(), "i": aid})

    def test_rotation_follows_the_request_path(self) -> None:
        uid, master = self.user(), self.master()
        first = self.request(uid, master)
        self.generate()
        self.api.fetchall("UPDATE agent_keys SET status = 'active', approved_at = now() WHERE id = CAST(:i AS uuid) "
                          "RETURNING id", {"i": first})
        # one live agent OR request per master: a second request while the first is live is refused
        with self.assertRaises(_db_errors()):
            self.request(uid, master)
        # rotation (api): the active agent → rotated, then the new request; the executor generates a NEW key
        self.api.fetchall("UPDATE agent_keys SET status = 'rotated', revoked_at = now() WHERE id = CAST(:i AS uuid) "
                          "RETURNING id", {"i": first})
        second = self.request(uid, master)
        self.generate()
        a, b = self.row(first), self.row(second)
        self.assertEqual(b["status"], "pending_approval")
        self.assertNotEqual(a["agent_address"], b["agent_address"])
        # terminal statuses are terminal (a rotated key can never come back)
        for status in ("active", "pending_approval"):
            with self.assertRaises(_db_errors()):
                self.api.fetchall("UPDATE agent_keys SET status = CAST(:s AS agent_key_status) "
                                  "WHERE id = CAST(:i AS uuid) RETURNING id", {"s": status, "i": first})

    def test_withdrawn_request_is_never_filled(self) -> None:
        uid, master = self.user(), self.master()
        aid = self.request(uid, master)
        import app.security.agent_keys as ak
        real = ak.generate_sealed_agent_key

        def sealing_then_withdrawn(enc: Any, *, user_id: str) -> Any:
            sk = real(enc, user_id=user_id)
            if user_id == uid:   # the user rotates / re-requests while the executor is sealing
                self.api.fetchall("UPDATE agent_keys SET status = 'revoked', revoked_at = now() "
                                  "WHERE id = CAST(:i AS uuid) RETURNING id", {"i": aid})
            return sk
        with mock.patch.object(ak, "generate_sealed_agent_key", sealing_then_withdrawn):
            rep = self.generate()
        self.assertGreaterEqual(rep["withdrawn"], 1)
        r = self.row(aid)
        self.assertEqual(r["status"], "revoked")
        self.assertIsNone(r["key_ciphertext"])
        # and the executor cannot fill a withdrawn request directly either (trigger)
        with self.assertRaises(_db_errors()):
            self.ex.fetchall("""UPDATE agent_keys SET agent_address = :a, key_ciphertext = '\\x01'::bytea,
                                       kms_key_version = 'v', keygen_at = now(), status = 'pending_approval'
                                 WHERE id = CAST(:i AS uuid) RETURNING id""", {"a": self.master(), "i": aid})

    def test_legacy_api_generated_key_is_never_attested_or_activated(self) -> None:
        """Rows sealed by the api before 0016 (keygen_at NULL): no attestation, no activation."""
        from app.execution.trust_jobs import attest_agents
        from app.security.agent_keys import generate_sealed_agent_key

        uid, master = self.user(), self.master()
        sk = generate_sealed_agent_key(self.enc, user_id=uid)
        aid = self.admin.fetchall("""
            INSERT INTO agent_keys (user_id, master_address, agent_address, key_ciphertext, kms_key_version, status)
            VALUES (CAST(:u AS uuid), :m, :a, :c, :v, 'pending_approval') RETURNING id::text AS id""",
                                  {"u": uid, "m": master, "a": sk.address, "c": sk.ciphertext, "v": sk.key_version})[0]["id"]
        attest_agents(self.db, T0, decryptor=self.dec, signer=self.signer, limit=1000)
        self.assertIsNone(self.row(aid)["attestation_sig"])
        with self.assertRaises(_db_errors()):
            self.ex.fetchall("UPDATE agent_keys SET attestation_sig = :s, attestation_key_version = 'k', "
                             "attested_at = now() WHERE id = CAST(:i AS uuid) RETURNING id", {"s": "A" * 88, "i": aid})
        with self.assertRaises(_db_errors()):
            self.api.fetchall("UPDATE agent_keys SET status = 'active' WHERE id = CAST(:i AS uuid) RETURNING id",
                              {"i": aid})
        self.api.fetchall("UPDATE agent_keys SET status = 'revoked' WHERE id = CAST(:i AS uuid) RETURNING id", {"i": aid})

    def test_jobs_wrappers_generate_then_attest_and_tick_hook(self) -> None:
        from app.execution import jobs

        settings = SimpleNamespace(env="test", is_prod=False, service_role="executor", kms_key_name="",
                                   local_dev_kek_b64=base64.b64encode(KEK).decode(), agent_attest_key_version="")
        rt = SimpleNamespace(settings=settings, _lock=threading.Lock(), _agent_decryptor=None, _agent_encryptor=None)
        uid = self.user()
        a1 = self.request(uid, self.master())
        rep = jobs.attest_agents(db=self.db, now=T0, runtime=rt, signer=self.signer, limit=1000)
        self.assertGreaterEqual(rep["keygen"]["generated"], 1, rep)
        self.assertEqual(self.row(a1)["status"], "pending_approval")
        self.assertIsNotNone(self.row(a1)["attestation_sig"])
        # the tick hook: no request waiting → nothing built, nothing touched
        self.assertIsNone(jobs.tick_generate_agents(db=self.db, now=T0, runtime=SimpleNamespace()))
        a2 = self.request(uid, self.master())
        with mock.patch.object(jobs, "_attest_signer", lambda _rt: self.signer):
            out = jobs.tick_generate_agents(db=self.db, now=T0, runtime=rt)
        self.assertIsNotNone(out)
        self.assertGreaterEqual(out["generated"], 1)
        r2 = self.row(a2)
        self.assertEqual(r2["status"], "pending_approval")
        self.assertTrue(kms.verify_p256_signature(self.signer.public_key_spki_der(),
                                                  kms.agent_attestation_message(uid, r2["agent_address"]),
                                                  base64.b64decode(r2["attestation_sig"])))
        # an api-role runtime can never build the sealing key
        api_rt = SimpleNamespace(settings=SimpleNamespace(**{**vars(settings), "service_role": "api"}),
                                 _lock=threading.Lock(), _agent_decryptor=None, _agent_encryptor=None)
        with self.assertRaises(Forbidden):
            jobs.generate_agents(db=self.db, now=T0, runtime=api_rt, decryptor=self.dec, signer=self.signer)

    def test_infra_verify_sql_asserts_the_privilege_model(self) -> None:
        verify = ROOT / "infra/gcp/sql/20_verify.sql"
        args = ["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-v", "api_user=app_api",
                "-v", "executor_user=app_executor"]
        ok = subprocess.run([*args, "-f", str(verify)], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        # a column grant that would let the api write key material is caught (never committed: the session stops on
        # the error inside the open transaction and disconnects → rollback)
        script = f"BEGIN;\nGRANT UPDATE (key_ciphertext) ON agent_keys TO app_api;\n\\i {verify}\nROLLBACK;\n"
        bad = subprocess.run([*args, "-f", "-"], input=script, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("api can write agent_keys.key_ciphertext", bad.stderr)
        still = self.admin.fetchall("SELECT has_column_privilege('app_api', 'agent_keys', 'key_ciphertext', 'UPDATE') AS x")
        self.assertFalse(still[0]["x"])


if __name__ == "__main__":
    unittest.main()
