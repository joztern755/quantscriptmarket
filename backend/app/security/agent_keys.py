"""Hyperliquid agent (API) wallet keys: generate, seal (API process), open (executor only). SPEC §5.3.

* Keys come from the OS CSPRNG (`secrets`), are validated to lie in [1, n-1] for secp256k1, and the address is
  derived as the last 20 bytes of keccak256(uncompressed_pubkey[1:]), returned lower-case.
* Keys are sealed with envelope encryption (`app.security.kms`) bound to (user_id, agent_address) before they
  ever reach the DB. Only the executor can open them; `open_agent_key` returns a bytearray which the caller
  must `zeroize()` as soon as the order is signed (use `opened_agent_key` context manager).
* Nothing in this module logs key material, and every holder's __repr__ hides it.
"""
from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.security.keccak import keccak256
from app.security.kms import DecryptionFailed, EnvelopeDecryptor, EnvelopeEncryptor, zeroize

__all__ = [
    "SECP256K1_N", "SealedKey", "generate_agent_key", "address_from_private_key", "to_checksum_address",
    "is_address", "seal_agent_key", "generate_sealed_agent_key", "open_agent_key", "opened_agent_key",
    "agent_key_aad", "zeroize",
]

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_AAD_CONTEXT = b"aijalon/agent_keys/v1"


def is_address(value: str) -> bool:
    """True for a lower-case 0x-prefixed 20-byte hex address (our canonical storage form)."""
    return isinstance(value, str) and bool(_ADDR_RE.match(value))


def address_from_private_key(priv: bytes | bytearray) -> str:
    if len(priv) != 32:
        raise ValueError("private key must be 32 bytes")
    k = int.from_bytes(priv, "big")
    if not 1 <= k < SECP256K1_N:
        raise ValueError("private key out of range for secp256k1")
    pub = ec.derive_private_key(k, ec.SECP256K1()).public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    del k
    return "0x" + keccak256(pub[1:])[-20:].hex()


def to_checksum_address(address: str) -> str:
    """EIP-55 mixed-case checksum form (for display / wallets that insist). Storage stays lower-case."""
    a = address.lower()
    if not is_address(a):
        raise ValueError("not an address")
    h = keccak256(a[2:].encode("ascii")).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(h[i], 16) >= 8 else c for i, c in enumerate(a[2:]))


def generate_agent_key() -> tuple[bytes, str]:
    """Return (private_key_bytes(32), lower-case address).

    Prefer `generate_sealed_agent_key`, which never hands the plaintext key to the caller. Python `bytes` cannot
    be wiped; drop the reference immediately after sealing."""
    while True:
        buf = bytearray(secrets.token_bytes(32))
        k = int.from_bytes(buf, "big")
        if 1 <= k < SECP256K1_N:  # probability of rejection ~ 2^-128
            break
        zeroize(buf)
    try:
        return bytes(buf), address_from_private_key(buf)
    finally:
        zeroize(buf)


def agent_key_aad(user_id: str, agent_address: str) -> bytes:
    """Record binding for the envelope: a sealed key only opens for the same (user_id, agent_address)."""
    uid = str(user_id).encode("utf-8")
    addr = agent_address.lower().encode("ascii")
    if not uid:
        raise ValueError("user_id required")
    if not is_address(agent_address.lower()):
        raise ValueError("agent_address must be a 0x-prefixed 20-byte hex address")
    return _AAD_CONTEXT + b"\x00" + len(uid).to_bytes(2, "big") + uid + len(addr).to_bytes(2, "big") + addr


@dataclass(frozen=True)
class SealedKey:
    """What goes into `agent_keys` (key_ciphertext, kms_key_version, agent_address). Safe to log/repr."""
    ciphertext: bytes
    key_version: str
    address: str

    def __repr__(self) -> str:
        return f"SealedKey(address={self.address!r}, key_version={self.key_version!r}, ciphertext=<{len(self.ciphertext)} bytes>)"


def seal_agent_key(priv: bytes | bytearray, encryptor: EnvelopeEncryptor, *, user_id: str) -> SealedKey:
    """Encrypt an agent key for storage. `user_id` is bound into the ciphertext (AAD)."""
    address = address_from_private_key(priv)
    sealed = encryptor.seal(priv, agent_key_aad(user_id, address))
    return SealedKey(ciphertext=sealed.blob, key_version=sealed.key_version, address=address)


def generate_sealed_agent_key(encryptor: EnvelopeEncryptor, *, user_id: str) -> SealedKey:
    """Generate + seal without exposing the plaintext key to the caller (API path for POST /agents)."""
    while True:
        buf = bytearray(secrets.token_bytes(32))
        if 1 <= int.from_bytes(buf, "big") < SECP256K1_N:
            break
        zeroize(buf)
    try:
        return seal_agent_key(buf, encryptor, user_id=user_id)
    finally:
        zeroize(buf)


def open_agent_key(sealed: SealedKey, decryptor: EnvelopeDecryptor, *, user_id: str) -> bytearray:
    """Decrypt (executor only). Returns a 32-byte bytearray; the CALLER MUST zeroize() it after signing.
    Re-derives the address and refuses if it does not match the stored agent address."""
    priv = decryptor.open(sealed.ciphertext, agent_key_aad(user_id, sealed.address))
    try:
        if len(priv) != 32 or address_from_private_key(priv) != sealed.address.lower():
            raise DecryptionFailed("decrypted key does not match stored agent address")
    except Exception:
        zeroize(priv)
        raise
    return priv


@contextmanager
def opened_agent_key(sealed: SealedKey, decryptor: EnvelopeDecryptor, *, user_id: str) -> Iterator[bytearray]:
    """with opened_agent_key(sk, dec, user_id=u) as priv: sign(...)  — zeroised on exit, even on error."""
    priv = open_agent_key(sealed, decryptor, user_id=user_id)
    try:
        yield priv
    finally:
        zeroize(priv)
