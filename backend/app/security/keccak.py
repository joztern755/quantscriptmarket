"""Keccak-256 as used by Ethereum (original Keccak padding 0x01, NOT FIPS-202 SHA3-256 padding 0x06).

`hashlib.sha3_256` is a DIFFERENT function and must never be used for Ethereum addresses.

`keccak256()` uses a fast native implementation when one is installed (pycryptodome's `Crypto.Hash.keccak`,
or `eth_hash`, both pulled in by eth-account in prod), and falls back to the pure-Python `keccak256_pure()`.
The pure version is slow (~ms per block) but has no dependencies; it is used in tests and dev.
"""
from __future__ import annotations

from typing import Callable

__all__ = ["keccak256", "keccak256_pure", "keccak256_hex"]

_MASK = (1 << 64) - 1
_RATE = 136  # bytes; 1600-bit state, capacity 512 bits for a 256-bit output

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)

# Rotation offsets r[x][y], indexed as lane index x + 5*y.
_ROT = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)


def _rol(v: int, n: int) -> int:
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _keccak_f(a: list[int]) -> None:
    """Keccak-f[1600] permutation in place. Lane (x, y) is a[x + 5*y]."""
    for rc in _RC:
        # θ
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for i in range(25):
            a[i] ^= d[i % 5]
        # ρ and π: B[y, 2x+3y] = rot(A[x, y], r[x, y])
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _ROT[x + 5 * y])
        # χ
        for y in range(0, 25, 5):
            row = b[y:y + 5]
            for x in range(5):
                a[y + x] = row[x] ^ ((~row[(x + 1) % 5]) & row[(x + 2) % 5])
        # ι
        a[0] ^= rc


def _sponge256(data: bytes | bytearray | memoryview, domain: int) -> bytes:
    """Keccak[c=512] sponge with a 256-bit output. domain=0x01 -> Ethereum Keccak-256; 0x06 -> FIPS-202 SHA3-256
    (the latter exists only so tests can cross-check the permutation against hashlib.sha3_256)."""
    msg = bytearray(data)
    pad_len = _RATE - (len(msg) % _RATE)
    if pad_len == 1:
        msg.append(domain | 0x80)
    else:
        msg.append(domain)
        msg.extend(b"\x00" * (pad_len - 2))
        msg.append(0x80)
    state = [0] * 25
    for off in range(0, len(msg), _RATE):
        block = msg[off:off + _RATE]
        for i in range(_RATE // 8):
            state[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        _keccak_f(state)
    out = b"".join(state[i].to_bytes(8, "little") for i in range(4))
    # best-effort wipe of the local copy (the input may be key material)
    for i in range(len(msg)):
        msg[i] = 0
    return out


def keccak256_pure(data: bytes | bytearray | memoryview) -> bytes:
    """Pure-Python Ethereum Keccak-256 (pad10*1 with domain bits 0x01)."""
    return _sponge256(data, 0x01)


def _select_backend() -> Callable[[bytes], bytes]:
    try:  # pycryptodome (eth-hash[pycryptodome], installed with eth-account)
        from Crypto.Hash import keccak as _ck  # type: ignore

        def _pycryptodome(data: bytes) -> bytes:
            return _ck.new(digest_bits=256, data=bytes(data)).digest()

        if _pycryptodome(b"") == keccak256_pure(b""):
            return _pycryptodome
    except Exception:  # noqa: BLE001 - optional accelerator
        pass
    try:
        from eth_hash.auto import keccak as _ek  # type: ignore

        def _eth_hash(data: bytes) -> bytes:
            return _ek(bytes(data))

        if _eth_hash(b"") == keccak256_pure(b""):
            return _eth_hash
    except Exception:  # noqa: BLE001
        pass
    return keccak256_pure


_backend: Callable[[bytes], bytes] | None = None


def keccak256(data: bytes | bytearray | memoryview) -> bytes:
    """Ethereum Keccak-256 (native backend if available, else pure Python). Self-checked at first use."""
    global _backend
    if _backend is None:
        _backend = _select_backend()
    return _backend(data)


def keccak256_hex(data: bytes | bytearray | memoryview) -> str:
    return keccak256(data).hex()
