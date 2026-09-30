"""REVIEW_TRADING_KEYS F2 — creator code sealed under a DEDICATED `creator-code` KMS key (SECURITY §3.4 (a)).

Reproduce → blocked:
* before: the API sealed creator code with the SAME encryptor/KEK as agent keys and the executor opened both with one
  decryptor on `agent-keys`; now `make_code_encryptor` / `make_code_decryptor` use `settings.creator_code_kms_key_name`
  with their own KMS wrap AAD and record-AAD namespace, so blobs never cross-open (even with one dev KEK);
* only the executor role can build the code decryptor; prod requires the key and refuses it being equal to agent-keys;
* the Cloud KMS requests go to the creator-code key with the creator-code AAD (fake KMS client);
* the executor's CreatorCodeDecryptor opens exactly what the API seals and refuses the old AAD scheme;
* infra: creator-code key created, api = encrypter only / executor = decrypter only on it; env wired to both services.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.errors import Forbidden  # noqa: E402
from app.execution.keys import CreatorCodeDecryptor  # noqa: E402
from app.security import kms  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
KEK = base64.b64encode(b"k" * 32).decode()
AGENT = "projects/p/locations/r/keyRings/aijalon/cryptoKeys/agent-keys"
CODE = "projects/p/locations/r/keyRings/aijalon/cryptoKeys/creator-code"


def settings(**kw):
    base = dict(env="test", kms_key_name="", creator_code_kms_key_name="", local_dev_kek_b64=KEK,
                service_role="executor")
    base.update(kw)
    return dataclasses.replace(get_settings(), **base)


class FakeKms:
    """Records requests; 'wraps' by XOR so unwrap works; returns the KMS response fields the wrapper checks."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    class _R:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def encrypt(self, request, timeout=None):
        self.calls.append(("encrypt", request))
        ct = bytes(b ^ 0x5A for b in request["plaintext"]) + request["additional_authenticated_data"][:4]
        return self._R(ciphertext=ct, ciphertext_crc32c=kms.crc32c(ct), verified_plaintext_crc32c=True,
                       verified_additional_authenticated_data_crc32c=True,
                       name=request["name"] + "/cryptoKeyVersions/1", protection_level=2)

    def decrypt(self, request, timeout=None):
        self.calls.append(("decrypt", request))
        ct = request["ciphertext"]
        if ct[-4:] != request["additional_authenticated_data"][:4]:
            raise RuntimeError("aad mismatch")
        pt = bytes(b ^ 0x5A for b in ct[:-4])
        return self._R(plaintext=pt, plaintext_crc32c=kms.crc32c(pt), protection_level=2)


class CodeKeySeparationTest(unittest.TestCase):
    def test_roundtrip_and_no_cross_open_with_one_dev_kek(self) -> None:
        s = settings()
        code_enc, code_dec = kms.make_code_encryptor(s), kms.make_code_decryptor(s)
        agent_enc, agent_dec = kms.make_encryptor(s), kms.make_decryptor(s)
        aad = kms.creator_code_aad("sid", "h" * 64)
        blob = code_enc.seal(b"def signal(bars): ...", aad).blob
        self.assertEqual(bytes(code_dec.open(blob, aad)), b"def signal(bars): ...")
        with self.assertRaises(kms.DecryptionFailed):       # agent-key decryptor cannot open creator code
            agent_dec.open(blob, aad)
        akey = agent_enc.seal(b"\x01" * 32, b"aijalon/agent_keys/v1:u:a").blob
        with self.assertRaises(kms.DecryptionFailed):       # and the code decryptor cannot open an agent key
            code_dec.open(akey, b"aijalon/agent_keys/v1:u:a")

    def test_record_aad_namespace(self) -> None:
        aad = kms.creator_code_aad("s1", "ab")
        self.assertTrue(aad.startswith(b"aijalon/creator_code/v1\x00"))
        self.assertNotEqual(aad, b"strategy_code:s1:ab")
        self.assertNotEqual(kms.CREATOR_CODE_KMS_AAD, kms.AGENT_KEY_KMS_AAD)

    def test_only_executor_builds_code_decryptor(self) -> None:
        for role in ("api", "sandbox", ""):
            with self.assertRaises(Forbidden):
                kms.make_code_decryptor(settings(service_role=role, env="prod", kms_key_name=AGENT,
                                                 creator_code_kms_key_name=CODE, local_dev_kek_b64=""),
                                        kms_client=FakeKms())
        kms.make_code_encryptor(settings(service_role="api"))   # api may encrypt

    def test_prod_requires_distinct_code_key(self) -> None:
        with self.assertRaises(RuntimeError):
            kms.make_code_encryptor(settings(env="prod", service_role="api", kms_key_name=AGENT,
                                             creator_code_kms_key_name="", local_dev_kek_b64=""),
                                    kms_client=FakeKms())
        with self.assertRaises(RuntimeError):
            kms.make_code_encryptor(settings(env="prod", service_role="api", kms_key_name=AGENT,
                                             creator_code_kms_key_name=AGENT, local_dev_kek_b64=""),
                                    kms_client=FakeKms())

    def test_cloud_kms_uses_creator_code_key_and_aad(self) -> None:
        fake = FakeKms()
        s = settings(env="prod", kms_key_name=AGENT, creator_code_kms_key_name=CODE, local_dev_kek_b64="")
        sealed = kms.make_code_encryptor(s, kms_client=fake).seal(b"code", kms.creator_code_aad("s", "h"))
        self.assertEqual(fake.calls[-1][1]["name"], CODE)
        self.assertEqual(fake.calls[-1][1]["additional_authenticated_data"], kms.CREATOR_CODE_KMS_AAD)
        self.assertTrue(sealed.key_version.startswith(CODE + "/"))
        dec = kms.make_code_decryptor(s, kms_client=fake)
        self.assertEqual(bytes(dec.open(sealed.blob, kms.creator_code_aad("s", "h"))), b"code")
        self.assertEqual(fake.calls[-1][1]["name"], CODE)
        kms.make_encryptor(s, kms_client=fake).seal(b"k" * 32, b"x")
        self.assertEqual(fake.calls[-1][1]["name"], AGENT)            # agent keys stay on agent-keys

    def test_prod_config_requires_code_key_for_api_and_executor(self) -> None:
        src = (ROOT / "backend/app/config.py").read_text()
        self.assertIn('required += ["creator_code_kms_key_name"]', src)
        self.assertIn("CREATOR_CODE_KMS_KEY_NAME must be a different KMS key", src)


class ExecutorOpensWhatApiSealsTest(unittest.TestCase):
    def test_executor_decryptor_matches_api_seal_and_rejects_old_scheme(self) -> None:
        s = settings()
        code = "def signal(bars):\n    return {}\n".encode()
        h = hashlib.sha256(code).hexdigest()
        blob = kms.make_code_encryptor(s).seal(code, kms.creator_code_aad("sid", h)).blob
        dec = CreatorCodeDecryptor(settings=s)
        self.assertEqual(dec.open_source(strategy_id="sid", code_hash=h, ciphertext=blob), code.decode())
        with self.assertRaises(kms.DecryptionFailed):           # bound to another strategy id
            dec.open_source(strategy_id="other", code_hash=h, ciphertext=blob)
        old = kms.make_encryptor(s).seal(code, f"strategy_code:sid:{h}".encode()).blob   # pre-fix: agent-keys KEK
        with self.assertRaises(kms.DecryptionFailed):
            dec.open_source(strategy_id="sid", code_hash=h, ciphertext=old)


def _have_fastapi() -> bool:
    try:
        import fastapi  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_fastapi(), "needs fastapi (app.api.adapters)")
class ApiAdapterTest(unittest.TestCase):
    def test_code_vault_uses_its_own_code_encryptor(self) -> None:
        from app.api import adapters

        svc = adapters.build_services(settings(service_role="api"))
        self.assertFalse(hasattr(svc, "agent_keys"))                        # the api has no agent-key path (0016)
        self.assertEqual(svc.code_vault._enc._factory, "make_code_encryptor")
        with self.assertRaises(ValueError):
            adapters._Encryptor(settings(service_role="api"), "make_encryptor")
        aad = kms.creator_code_aad("sid", "h")
        ct, _ = svc.code_vault.seal(b"code", aad)
        self.assertEqual(bytes(kms.make_code_decryptor(settings()).open(ct, aad)), b"code")
        with self.assertRaises(kms.DecryptionFailed):
            kms.make_decryptor(settings()).open(ct, aad)


class InfraTest(unittest.TestCase):
    def test_bootstrap_creates_key_with_split_iam(self) -> None:
        env = (ROOT / "infra/gcp/env.sh").read_text()
        boot = (ROOT / "infra/gcp/bootstrap.sh").read_text()
        self.assertIn('KMS_CODE_KEY:=creator-code', env)
        self.assertIn('KMS_CODE_KEY_NAME=', env)
        self.assertRegex(boot, r'gcloud kms keys create "\$\{KMS_CODE_KEY\}"[^\n]*\\\n[^\n]*--protection-level=hsm')
        start = boot.index("# creator-code: api ENCRYPT only")
        body = boot[start:boot.index("# api + executor: Cloud SQL client", start)]
        # creator-code: api encrypt, executor decrypt
        self.assertRegex(body, r'add-iam-policy-binding "\$\{KMS_CODE_KEY\}"[^\n]*\\\n\s*--member="serviceAccount:\$\{SA_API\}" '
                               r'--role=roles/cloudkms.cryptoKeyEncrypter')
        self.assertRegex(body, r'add-iam-policy-binding "\$\{KMS_CODE_KEY\}"[^\n]*\\\n\s*--member="serviceAccount:\$\{SA_EXECUTOR\}" '
                               r'--role=roles/cloudkms.cryptoKeyDecrypter')
        # agent-keys (0016): executor encrypt + decrypt, the api binding is REMOVED, never EncrypterDecrypter
        self.assertIn('for role in roles/cloudkms.cryptoKeyEncrypter roles/cloudkms.cryptoKeyDecrypter; do', body)
        self.assertRegex(body, r'add-iam-policy-binding "\$\{KMS_KEY\}"[^\n]*\\\n\s*--member="serviceAccount:\$\{SA_EXECUTOR\}" '
                               r'--role="\$\{role\}"')
        self.assertRegex(body, r'remove-iam-policy-binding "\$\{KMS_KEY\}"[^\n]*\\\n\s*--member="serviceAccount:\$\{SA_API\}"')
        self.assertNotRegex(body, r'add-iam-policy-binding "\$\{KMS_KEY\}"[^\n]*\\\n\s*--member="serviceAccount:\$\{SA_API\}"')
        self.assertNotIn("EncrypterDecrypter", body)

    def test_services_get_the_key_name(self) -> None:
        for svc in ("api", "executor"):
            y = (ROOT / f"infra/gcp/run/{svc}.service.yaml").read_text()
            self.assertIn('{name: CREATOR_CODE_KMS_KEY_NAME, value: "${KMS_CODE_KEY_NAME}"}', y, svc)
        self.assertIn("KMS_CODE_KEY_NAME", (ROOT / "infra/gcp/deploy.sh").read_text())


if __name__ == "__main__":
    unittest.main()
