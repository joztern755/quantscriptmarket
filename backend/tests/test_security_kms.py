from __future__ import annotations

import base64
import os
import unittest
from types import SimpleNamespace

try:  # the cryptography native backend is broken on some dev boxes (pyo3 panic); never skip in CI
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except BaseException as e:  # noqa: BLE001 - pyo3 PanicException derives from BaseException
    if isinstance(e, (KeyboardInterrupt, SystemExit)) or os.environ.get("CI"):
        raise
    raise unittest.SkipTest(f"cryptography unavailable: {type(e).__name__}") from None

from app.errors import ExternalServiceError, Forbidden
from app.security import kms
from app.security.kms import (
    AGENT_KEY_KMS_AAD, CloudKmsKeyWrapper, DecryptionFailed, EnvelopeDecryptor, EnvelopeEncryptor,
    LocalAesKeyWrapper, crc32c, make_decryptor, make_encryptor, zeroize,
)

KEY_NAME = "projects/p/locations/asia-southeast1/keyRings/aijalon/cryptoKeys/agent-keys"


def settings(**kw):
    base = dict(env="test", is_prod=False, kms_key_name="", local_dev_kek_b64=base64.b64encode(b"k" * 32).decode(),
                service_role="api")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeKms:
    """Mimics KeyManagementServiceClient.encrypt/decrypt incl. CRC32C fields and AAD enforcement."""

    def __init__(self, protection_level=2, version=1):
        self._aead = AESGCM(os.urandom(32))
        self.protection_level = protection_level
        self.version = version
        self.requests = []
        self.corrupt = None  # name of field to corrupt in the next response

    def encrypt(self, request, timeout=None):
        self.requests.append(("encrypt", request))
        pt, aad = request["plaintext"], request["additional_authenticated_data"]
        ok_pt = request["plaintext_crc32c"] == crc32c(pt)
        ok_aad = request["additional_authenticated_data_crc32c"] == crc32c(aad)
        nonce = os.urandom(12)
        ct = nonce + self._aead.encrypt(nonce, pt, aad)
        r = SimpleNamespace(name=f"{request['name']}/cryptoKeyVersions/{self.version}", ciphertext=ct,
                            ciphertext_crc32c=crc32c(ct), verified_plaintext_crc32c=ok_pt,
                            verified_additional_authenticated_data_crc32c=ok_aad, protection_level=self.protection_level)
        if self.corrupt == "ciphertext_crc32c":
            r.ciphertext_crc32c ^= 1
        elif self.corrupt == "verified_plaintext_crc32c":
            r.verified_plaintext_crc32c = False
        elif self.corrupt == "verified_aad":
            r.verified_additional_authenticated_data_crc32c = False
        elif self.corrupt == "name":
            r.name = "projects/evil/locations/x/keyRings/y/cryptoKeys/z/cryptoKeyVersions/1"
        self.corrupt = None
        return r

    def decrypt(self, request, timeout=None):
        self.requests.append(("decrypt", request))
        ct, aad = request["ciphertext"], request["additional_authenticated_data"]
        if request["ciphertext_crc32c"] != crc32c(ct):
            raise RuntimeError("400 checksum mismatch")
        pt = self._aead.decrypt(ct[:12], ct[12:], aad)
        r = SimpleNamespace(plaintext=pt, plaintext_crc32c=crc32c(pt), protection_level=self.protection_level)
        if self.corrupt == "plaintext_crc32c":
            r.plaintext_crc32c ^= 1
        self.corrupt = None
        return r


class Crc32cTests(unittest.TestCase):
    def test_check_value(self):
        self.assertEqual(crc32c(b"123456789"), 0xE3069283)
        self.assertEqual(crc32c(b""), 0)


class ZeroizeTests(unittest.TestCase):
    def test_zeroize_in_place(self):
        b = bytearray(b"secret")
        view = memoryview(b)
        zeroize(b)
        self.assertEqual(bytes(view), b"\x00" * 6)
        zeroize(None)


class LocalEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.enc = EnvelopeEncryptor(LocalAesKeyWrapper(b"k" * 32, is_prod=False))
        self.dec = EnvelopeDecryptor(LocalAesKeyWrapper(b"k" * 32, is_prod=False, allow_unwrap=True))
        self.aad = b"user:1|agent:0xabc"

    def test_roundtrip_and_randomness(self):
        s1 = self.enc.seal(b"hello", self.aad)
        s2 = self.enc.seal(b"hello", self.aad)
        self.assertNotEqual(s1.blob, s2.blob)
        out = self.dec.open(s1.blob, self.aad)
        self.assertIsInstance(out, bytearray)
        self.assertEqual(out, b"hello")
        self.assertTrue(s1.key_version.startswith("local-dev:"))
        self.assertEqual(s1.blob[0], 0x01)
        self.assertEqual(s1.blob[1], kms.WRAPPER_KIND_LOCAL_DEV)

    def test_record_binding(self):
        s = self.enc.seal(b"hello", self.aad)
        with self.assertRaises(DecryptionFailed):
            self.dec.open(s.blob, b"user:2|agent:0xabc")

    def test_tamper_every_region(self):
        blob = self.enc.seal(b"hello world", self.aad).blob
        wlen = int.from_bytes(blob[2:4], "big")
        positions = [0, 1, 4, 4 + wlen - 1, 4 + wlen, 4 + wlen + 11, 4 + wlen + 12, len(blob) - 1]
        for pos in positions:
            t = bytearray(blob)
            t[pos] ^= 0x01
            with self.assertRaises(DecryptionFailed, msg=f"pos {pos}"):
                self.dec.open(bytes(t), self.aad)
        for cut in (0, 3, 4 + wlen, len(blob) - 1):
            with self.assertRaises(DecryptionFailed):
                self.dec.open(blob[:cut], self.aad)

    def test_wrong_kek(self):
        s = self.enc.seal(b"x", self.aad)
        other = EnvelopeDecryptor(LocalAesKeyWrapper(b"z" * 32, is_prod=False, allow_unwrap=True))
        with self.assertRaises(DecryptionFailed):
            other.open(s.blob, self.aad)

    def test_requires_aad_and_size(self):
        with self.assertRaises(ValueError):
            self.enc.seal(b"x", b"")
        with self.assertRaises(ValueError):
            self.enc.seal(b"", self.aad)


class SegregationTests(unittest.TestCase):
    def test_encryptor_has_no_decrypt(self):
        enc = make_encryptor(settings(service_role="executor"))
        for name in ("open", "decrypt", "unwrap"):
            self.assertFalse(hasattr(enc, name), name)
        with self.assertRaises(Forbidden):
            enc._wrapper.unwrap(b"\x00" * 60)

    def test_decryptor_needs_unwrap_capable_wrapper(self):
        with self.assertRaises(Forbidden):
            EnvelopeDecryptor(LocalAesKeyWrapper(b"k" * 32, is_prod=False))

    def test_make_decryptor_role(self):
        with self.assertRaises(Forbidden):
            make_decryptor(settings(service_role="api"))
        with self.assertRaises(Forbidden):
            make_decryptor(SimpleNamespace(env="test", is_prod=False, kms_key_name="", local_dev_kek_b64=""))
        d = make_decryptor(settings(service_role="executor"))
        e = make_encryptor(settings(service_role="executor"))
        self.assertEqual(d.open(e.seal(b"x", b"a").blob, b"a"), b"x")
        make_decryptor(settings(service_role="all"))  # single-process dev

    def test_prod_rules(self):
        prod = dict(env="prod", is_prod=True)
        with self.assertRaises(RuntimeError):
            LocalAesKeyWrapper(b"k" * 32, is_prod=True)
        with self.assertRaises(RuntimeError):
            LocalAesKeyWrapper.from_settings(settings(**prod))
        with self.assertRaises(RuntimeError):
            make_encryptor(settings(**prod, service_role="executor"))  # no KMS key in prod
        with self.assertRaises(Forbidden):
            make_decryptor(settings(**prod, kms_key_name=KEY_NAME, service_role="all"), kms_client=FakeKms())
        fake = FakeKms()
        enc = make_encryptor(settings(**prod, kms_key_name=KEY_NAME, service_role="executor"), kms_client=fake)
        dec = make_decryptor(settings(**prod, kms_key_name=KEY_NAME, service_role="executor"), kms_client=fake)
        self.assertEqual(dec.open(enc.seal(b"k", b"a").blob, b"a"), b"k")

    def test_local_blob_refused_by_kms_decryptor(self):
        blob = EnvelopeEncryptor(LocalAesKeyWrapper(b"k" * 32, is_prod=False)).seal(b"x", b"a").blob
        dec = EnvelopeDecryptor(CloudKmsKeyWrapper(KEY_NAME, client=FakeKms(), allow_unwrap=True))
        with self.assertRaises(DecryptionFailed):
            dec.open(blob, b"a")

    def test_missing_dev_kek(self):
        with self.assertRaises(RuntimeError):
            make_encryptor(settings(local_dev_kek_b64="", service_role="executor"))

    def test_make_encryptor_role_executor_only(self):
        """migrations/0016: agent keys are generated + sealed by the executor; the api cannot build an agent-key
        encryptor at all (it would know the plaintext of every key it sealed)."""
        for role in ("api", "sandbox", None, ""):
            with self.assertRaises(Forbidden):
                make_encryptor(settings(service_role=role))
        with self.assertRaises(Forbidden):
            make_encryptor(settings(env="prod", is_prod=True, kms_key_name=KEY_NAME, service_role="all"),
                           kms_client=FakeKms())
        make_encryptor(settings(service_role="executor"))
        make_encryptor(settings(service_role="all"))   # single-process dev only

    def test_reprs_hide_material(self):
        w = LocalAesKeyWrapper(b"k" * 32, is_prod=False)
        self.assertNotIn("kkkk", repr(w))
        s = EnvelopeEncryptor(w).seal(b"secret-plaintext", b"a")
        self.assertNotIn(s.blob.hex()[:16], repr(s))


class CloudKmsTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeKms()
        self.w = CloudKmsKeyWrapper(KEY_NAME, client=self.fake, allow_unwrap=True)

    def test_roundtrip_and_request_integrity_fields(self):
        dek = os.urandom(32)
        wrapped, version = self.w.wrap(dek)
        self.assertEqual(version, KEY_NAME + "/cryptoKeyVersions/1")
        _, req = self.fake.requests[-1]
        self.assertEqual(req["name"], KEY_NAME)
        self.assertEqual(req["additional_authenticated_data"], AGENT_KEY_KMS_AAD)
        self.assertEqual(req["additional_authenticated_data_crc32c"], crc32c(AGENT_KEY_KMS_AAD))
        self.assertEqual(req["plaintext_crc32c"], crc32c(dek))
        self.assertEqual(self.w.unwrap(wrapped), dek)
        _, dreq = self.fake.requests[-1]
        self.assertEqual(dreq["ciphertext_crc32c"], crc32c(wrapped))
        self.assertEqual(dreq["additional_authenticated_data"], AGENT_KEY_KMS_AAD)

    def test_response_integrity_failures(self):
        for field in ("ciphertext_crc32c", "verified_plaintext_crc32c", "verified_aad", "name"):
            self.fake.corrupt = field
            with self.assertRaises(ExternalServiceError, msg=field):
                self.w.wrap(os.urandom(32))
        wrapped, _ = self.w.wrap(os.urandom(32))
        self.fake.corrupt = "plaintext_crc32c"
        with self.assertRaises(ExternalServiceError):
            self.w.unwrap(wrapped)

    def test_requires_hsm(self):
        soft = CloudKmsKeyWrapper(KEY_NAME, client=FakeKms(protection_level=1), allow_unwrap=True)
        with self.assertRaises(ExternalServiceError):
            soft.wrap(os.urandom(32))
        CloudKmsKeyWrapper(KEY_NAME, client=FakeKms(protection_level=1), require_hsm=False).wrap(os.urandom(32))

    def test_client_error_wrapped(self):
        class Boom:
            def encrypt(self, request, timeout=None):
                raise RuntimeError("PERMISSION_DENIED")
        with self.assertRaises(ExternalServiceError):
            CloudKmsKeyWrapper(KEY_NAME, client=Boom()).wrap(os.urandom(32))

    def test_unwrap_forbidden_without_permission(self):
        w = CloudKmsKeyWrapper(KEY_NAME, client=self.fake)
        wrapped, _ = w.wrap(os.urandom(32))
        with self.assertRaises(Forbidden):
            w.unwrap(wrapped)
        self.assertFalse(any(k == "decrypt" for k, _ in self.fake.requests))

    def test_key_name_validation(self):
        for bad in ("", "agent-keys", KEY_NAME + "/cryptoKeyVersions/1"):
            with self.assertRaises(ValueError):
                CloudKmsKeyWrapper(bad, client=self.fake)

    def test_wrong_dek_size(self):
        with self.assertRaises(ValueError):
            self.w.wrap(b"short")

    def test_envelope_over_kms(self):
        enc, dec = EnvelopeEncryptor(self.w), EnvelopeDecryptor(self.w)
        s = enc.seal(b"payload", b"bind")
        self.assertEqual(s.blob[1], kms.WRAPPER_KIND_CLOUD_KMS)
        self.assertEqual(dec.open(s.blob, b"bind"), b"payload")


if __name__ == "__main__":
    unittest.main()
