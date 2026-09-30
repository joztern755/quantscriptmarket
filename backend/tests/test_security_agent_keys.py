from __future__ import annotations

import io
import logging
import os
import unittest

try:  # the cryptography native backend is broken on some dev boxes (pyo3 panic); never skip in CI
    import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
except BaseException as e:  # noqa: BLE001
    if isinstance(e, (KeyboardInterrupt, SystemExit)) or os.environ.get("CI"):
        raise
    raise unittest.SkipTest(f"cryptography unavailable: {type(e).__name__}") from None

from app.logging import JsonFormatter
from app.security.agent_keys import (
    SECP256K1_N, SealedKey, address_from_private_key, generate_agent_key, generate_sealed_agent_key, is_address,
    open_agent_key, opened_agent_key, seal_agent_key, to_checksum_address,
)
from app.security.kms import DecryptionFailed, EnvelopeDecryptor, EnvelopeEncryptor, LocalAesKeyWrapper

VECTOR_PRIV = bytes.fromhex("4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318")
VECTOR_ADDR = "0x2c7536e3605d9c16a7a3d7b1898e529396a65c23"


class AddressTests(unittest.TestCase):
    def test_known_vector(self):
        self.assertEqual(address_from_private_key(VECTOR_PRIV), VECTOR_ADDR)

    def test_range(self):
        for bad in (b"\x00" * 32, SECP256K1_N.to_bytes(32, "big"), b"\xff" * 32, b"\x01" * 31):
            with self.assertRaises(ValueError):
                address_from_private_key(bad)
        self.assertTrue(is_address(address_from_private_key((SECP256K1_N - 1).to_bytes(32, "big"))))

    def test_eip55(self):
        for a in ("0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed", "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359",
                  "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB", "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"):
            self.assertEqual(to_checksum_address(a.lower()), a)
        with self.assertRaises(ValueError):
            to_checksum_address("0x123")

    def test_generate(self):
        seen = set()
        for _ in range(5):
            priv, addr = generate_agent_key()
            self.assertEqual(len(priv), 32)
            self.assertTrue(is_address(addr))
            self.assertEqual(addr, address_from_private_key(priv))
            seen.add(addr)
        self.assertEqual(len(seen), 5)


class SealTests(unittest.TestCase):
    def setUp(self):
        self.enc = EnvelopeEncryptor(LocalAesKeyWrapper(b"k" * 32, is_prod=False))
        self.dec = EnvelopeDecryptor(LocalAesKeyWrapper(b"k" * 32, is_prod=False, allow_unwrap=True))

    def test_roundtrip(self):
        sk = seal_agent_key(VECTOR_PRIV, self.enc, user_id="u1")
        self.assertEqual(sk.address, VECTOR_ADDR)
        priv = open_agent_key(sk, self.dec, user_id="u1")
        self.assertIsInstance(priv, bytearray)
        self.assertEqual(bytes(priv), VECTOR_PRIV)

    def test_bound_to_user(self):
        sk = seal_agent_key(VECTOR_PRIV, self.enc, user_id="u1")
        with self.assertRaises(DecryptionFailed):
            open_agent_key(sk, self.dec, user_id="u2")

    def test_bound_to_address(self):
        a = seal_agent_key(VECTOR_PRIV, self.enc, user_id="u1")
        b = generate_sealed_agent_key(self.enc, user_id="u1")
        swapped = SealedKey(ciphertext=a.ciphertext, key_version=a.key_version, address=b.address)
        with self.assertRaises(DecryptionFailed):
            open_agent_key(swapped, self.dec, user_id="u1")

    def test_generate_sealed(self):
        sk = generate_sealed_agent_key(self.enc, user_id="u9")
        priv = open_agent_key(sk, self.dec, user_id="u9")
        self.assertEqual(address_from_private_key(priv), sk.address)

    def test_context_manager_zeroizes(self):
        sk = seal_agent_key(VECTOR_PRIV, self.enc, user_id="u1")
        with opened_agent_key(sk, self.dec, user_id="u1") as priv:
            self.assertEqual(bytes(priv), VECTOR_PRIV)
            held = priv
        self.assertEqual(bytes(held), b"\x00" * 32)
        try:
            with opened_agent_key(sk, self.dec, user_id="u1") as priv:
                held = priv
                raise RuntimeError("signing failed")
        except RuntimeError:
            pass
        self.assertEqual(bytes(held), b"\x00" * 32)

    def test_repr_and_logs_never_show_key(self):
        sk = seal_agent_key(VECTOR_PRIV, self.enc, user_id="u1")
        r = repr(sk)
        self.assertNotIn(VECTOR_PRIV.hex(), r)
        self.assertNotIn(sk.ciphertext.hex()[:20], r)
        self.assertIn(VECTOR_ADDR, r)
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        h.setFormatter(JsonFormatter())
        lg = logging.getLogger("test.agent_keys")
        lg.addHandler(h)
        lg.propagate = False
        lg.warning("sealed %r priv %s", sk, VECTOR_PRIV.hex())
        out = stream.getvalue()
        self.assertNotIn(VECTOR_PRIV.hex(), out)
        self.assertIn("[REDACTED]", out)


if __name__ == "__main__":
    unittest.main()
