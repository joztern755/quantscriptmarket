"""EIP-191 personal_sign recovery (secp256k1 public-key recovery), stdlib + app.security.keccak only.

Used by the wallet-verification route when `eth_account` is not installed. Verification only (no private
keys), so constant-time is not a concern. Rejects high-s signatures (EIP-2) and invalid recovery ids.
"""
from __future__ import annotations

from app.security.keccak import keccak256

# secp256k1 domain parameters
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

_Point = tuple[int, int] | None


def _add(p: _Point, q: _Point) -> _Point:
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        lam = (3 * p[0] * p[0]) * pow(2 * p[1], -1, _P) % _P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (lam * lam - p[0] - q[0]) % _P
    return x, (lam * (p[0] - x) - p[1]) % _P


def _mul(k: int, p: _Point) -> _Point:
    r: _Point = None
    while k:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def personal_message_hash(message: str) -> bytes:
    data = message.encode("utf-8")
    return keccak256(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)


def recover_address(msg_hash: bytes, signature: bytes) -> str:
    """65-byte r||s||v signature over a 32-byte hash → lower-case 0x address. Raises ValueError if invalid."""
    if len(msg_hash) != 32 or len(signature) != 65:
        raise ValueError("bad hash or signature length")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:64], "big")
    v = signature[64]
    if v >= 27:
        v -= 27
    if v not in (0, 1):
        raise ValueError("bad recovery id")
    if not (1 <= r < _N and 1 <= s <= _N // 2):
        raise ValueError("signature out of range (or high-s)")
    x = r
    alpha = (pow(x, 3, _P) + 7) % _P
    beta = pow(alpha, (_P + 1) // 4, _P)
    if beta * beta % _P != alpha:
        raise ValueError("invalid signature point")
    y = beta if beta % 2 == v else _P - beta
    R = (x, y)
    e = int.from_bytes(msg_hash, "big") % _N
    r_inv = pow(r, -1, _N)
    q = _mul(r_inv, _add(_mul(s, R), _mul((-e) % _N, (_GX, _GY))))
    if q is None:
        raise ValueError("invalid signature")
    pub = q[0].to_bytes(32, "big") + q[1].to_bytes(32, "big")
    return "0x" + keccak256(pub)[-20:].hex()


def recover_personal_sign(message: str, signature_hex: str) -> str:
    sig = signature_hex[2:] if signature_hex.startswith("0x") else signature_hex
    try:
        raw = bytes.fromhex(sig)
    except ValueError as e:
        raise ValueError("signature is not hex") from e
    return recover_address(personal_message_hash(message), raw)
