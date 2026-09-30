from __future__ import annotations

import hashlib
import unittest

from app.security.keccak import _sponge256, keccak256, keccak256_hex, keccak256_pure


class KeccakTests(unittest.TestCase):
    def test_vectors(self):
        self.assertEqual(keccak256_pure(b"").hex(), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(keccak256_pure(b"abc").hex(), "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")
        self.assertEqual(keccak256_hex(b""), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_not_sha3(self):
        self.assertNotEqual(keccak256_pure(b"abc"), hashlib.sha3_256(b"abc").digest())

    def test_permutation_matches_fips202_at_block_boundaries(self):
        # Same Keccak-f[1600] with SHA3 domain padding must equal hashlib.sha3_256 for every length around the
        # 136-byte rate (covers the single-byte 0x81 pad case at len % 136 == 135).
        data = bytes(range(256)) * 4
        for n in list(range(0, 300)) + [543, 544, 545, 1000]:
            self.assertEqual(_sponge256(data[:n], 0x06), hashlib.sha3_256(data[:n]).digest(), n)

    def test_accepts_bytes_like(self):
        self.assertEqual(keccak256(bytearray(b"abc")), keccak256_pure(b"abc"))
        self.assertEqual(keccak256(memoryview(b"abc")), keccak256_pure(b"abc"))


if __name__ == "__main__":
    unittest.main()
