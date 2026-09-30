"""app.hl.typed_data: SPEC §6 payloads + EIP-712 hashing pinned to the EIP-712 specification test vector."""
from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.hl import typed_data as td  # noqa: E402
from app.security.keccak import keccak256  # noqa: E402

AGENT = "0x" + "4" * 40
BUILDER = "0x" + "b" * 40
TREASURY = "0x" + "7" * 40
NONCE = 1_790_000_000_123
ARB = 42161  # 0xa4b1

# --- minimal secp256k1 (test-only) to recover signers ---------------------------------------------------------
_P = 2 ** 256 - 2 ** 32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    lam = (3 * a[0] * a[0] * pow(2 * a[1], -1, _P)) % _P if a == b else ((b[1] - a[1]) * pow(b[0] - a[0], -1, _P)) % _P
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _mul(k, pt):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt, k = _add(pt, pt), k >> 1
    return r


def _address(pub) -> str:
    return "0x" + keccak256(pub[0].to_bytes(32, "big") + pub[1].to_bytes(32, "big"))[-20:].hex()


def _recover(digest: bytes, r: int, s: int, v: int) -> str:
    y = pow((pow(r, 3, _P) + 7) % _P, (_P + 1) // 4, _P)
    if (y & 1) != ((v - 27) & 1):
        y = _P - y
    z = int.from_bytes(digest, "big")
    return _address(_mul(pow(r, -1, _N), _add(_mul(s, (r, y)), _mul((-z) % _N, _G))))


def _sign(digest: bytes, priv: int, k: int) -> tuple[int, int, int]:
    R = _mul(k, _G)
    r = R[0] % _N
    s = pow(k, -1, _N) * (int.from_bytes(digest, "big") + r * priv) % _N
    v = 27 + (R[1] & 1)
    if s > _N // 2:
        s, v = _N - s, 55 - v
    return r, s, v


class Eip712Vector(unittest.TestCase):
    """The "Ether Mail" example from EIP-712 (domain separator, struct hash, digest, signature by keccak('cow'))."""

    MAIL = {
        "types": {"EIP712Domain": td.EIP712_DOMAIN_TYPES,
                  "Person": [{"name": "name", "type": "string"}, {"name": "wallet", "type": "address"}],
                  "Mail": [{"name": "from", "type": "Person"}, {"name": "to", "type": "Person"},
                           {"name": "contents", "type": "string"}]},
        "primaryType": "Mail",
        "domain": {"name": "Ether Mail", "version": "1", "chainId": 1,
                   "verifyingContract": "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC"},
        "message": {"from": {"name": "Cow", "wallet": "0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826"},
                    "to": {"name": "Bob", "wallet": "0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB"},
                    "contents": "Hello, Bob!"},
    }

    def test_vector(self) -> None:
        t = self.MAIL["types"]
        self.assertEqual(td.hash_struct("EIP712Domain", self.MAIL["domain"], t).hex(),
                         "f2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f")
        self.assertEqual(td.hash_struct("Mail", self.MAIL["message"], t).hex(),
                         "c52c0ee5d84264471806290a3f2c4cecfc5490626bf912d01f240d7a274b371e")
        digest = td.hash_typed_data(self.MAIL)
        self.assertEqual(digest.hex(), "be609aee343fb3c4b28e1df9e632fca64fcfaede20f02e86244efddf30957bd2")
        r = 0x4355c47d63924e8a72e509b65029052eb6c299d53a04e167c5775fd466751c9d
        s = 0x07299936d304c153f6443dfa05f40ff007d72911b6f72307f996231605b91562
        self.assertEqual(_recover(digest, r, s, 28), "0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826")
        self.assertEqual(_address(_mul(int.from_bytes(keccak256(b"cow"), "big"), _G)),
                         "0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826")


class Payloads(unittest.TestCase):
    def test_approve_agent(self) -> None:
        req = td.approve_agent_request(AGENT.upper().replace("0X", "0x"), nonce_ms=NONCE, signature_chain_id=ARB)
        t = req.typed_data
        self.assertEqual(t["domain"], {"name": "HyperliquidSignTransaction", "version": "1", "chainId": 42161,
                                       "verifyingContract": "0x0000000000000000000000000000000000000000"})
        self.assertEqual(t["primaryType"], "HyperliquidTransaction:ApproveAgent")
        self.assertEqual(t["types"]["HyperliquidTransaction:ApproveAgent"], [
            {"name": "hyperliquidChain", "type": "string"}, {"name": "agentAddress", "type": "address"},
            {"name": "agentName", "type": "string"}, {"name": "nonce", "type": "uint64"}])
        self.assertEqual(t["types"]["EIP712Domain"][2], {"name": "chainId", "type": "uint256"})
        self.assertEqual(t["message"], {"hyperliquidChain": "Mainnet", "agentAddress": AGENT, "agentName": "aijalon",
                                        "nonce": NONCE})
        self.assertEqual(req.action, {"type": "approveAgent", "signatureChainId": "0xa4b1",
                                      "hyperliquidChain": "Mainnet", "agentAddress": AGENT, "agentName": "aijalon",
                                      "nonce": NONCE})
        self.assertEqual(req.nonce, NONCE)
        for k, v in t["message"].items():  # the action must carry exactly what was signed
            self.assertEqual(req.action[k], v)
        testnet = td.approve_agent_request(AGENT, nonce_ms=NONCE, signature_chain_id="0x66eee", is_mainnet=False)
        self.assertEqual((testnet.typed_data["domain"]["chainId"], testnet.action["hyperliquidChain"]),
                         (421614, "Testnet"))

    def test_agent_name_rules(self) -> None:
        for bad in ("", "x" * 17, "a;b"):
            with self.assertRaises(ValidationFailed):
                td.approve_agent_request(AGENT, nonce_ms=NONCE, signature_chain_id=ARB, agent_name=bad)
        req = td.approve_agent_request(AGENT, nonce_ms=NONCE, signature_chain_id=ARB, valid_until_ms=NONCE + 86400000)
        self.assertEqual(req.action["agentName"], f"aijalon valid_until {NONCE + 86400000}")

    def test_approve_builder_fee(self) -> None:
        req = td.approve_builder_fee_request(BUILDER, nonce_ms=NONCE, signature_chain_id="0xA4B1")
        self.assertEqual(req.typed_data["types"]["HyperliquidTransaction:ApproveBuilderFee"], [
            {"name": "hyperliquidChain", "type": "string"}, {"name": "maxFeeRate", "type": "string"},
            {"name": "builder", "type": "address"}, {"name": "nonce", "type": "uint64"}])
        self.assertEqual(req.action, {"type": "approveBuilderFee", "signatureChainId": "0xa4b1",
                                      "hyperliquidChain": "Mainnet", "maxFeeRate": "0.1%", "builder": BUILDER,
                                      "nonce": NONCE})

    def test_fee_rate_strings(self) -> None:
        self.assertEqual([td.fee_rate_percent(x) for x in (100, 50, 10, 1, 25)],
                         ["0.1%", "0.05%", "0.01%", "0.001%", "0.025%"])
        for bad in (0, 101, -1, True):
            with self.assertRaises(ValidationFailed):
                td.fee_rate_percent(bad)  # type: ignore[arg-type]

    def test_usd_send(self) -> None:
        req = td.usd_send_request(TREASURY, "10.5", time_ms=NONCE, signature_chain_id=ARB)
        self.assertEqual(req.typed_data["types"]["HyperliquidTransaction:UsdSend"], [
            {"name": "hyperliquidChain", "type": "string"}, {"name": "destination", "type": "string"},
            {"name": "amount", "type": "string"}, {"name": "time", "type": "uint64"}])
        self.assertEqual(req.action, {"type": "usdSend", "signatureChainId": "0xa4b1", "hyperliquidChain": "Mainnet",
                                      "destination": TREASURY, "amount": "10.5", "time": NONCE})
        for bad in ("10.50", "010", "-1", "0", "1e3", "1.1234567", 10):
            with self.assertRaises(ValidationFailed, msg=bad):
                td.usd_send_request(TREASURY, bad, time_ms=NONCE, signature_chain_id=ARB)  # type: ignore[arg-type]

    def test_payments_delegation_contract(self) -> None:
        inspect.signature(td.usd_send_typed_data).bind(destination="", amount="", time_ms=0, signature_chain_id="0x1",
                                                       is_mainnet=True)
        from app.payments import usdc

        self.assertIs(usdc._typed_data_builder(), td.usd_send_typed_data)
        a = td.usd_send_typed_data(destination=TREASURY, amount="10", time_ms=NONCE, signature_chain_id="0xa4b1",
                                   is_mainnet=True)
        b = usdc.usd_send_typed_data(destination=TREASURY, amount="10", time_ms=NONCE, signature_chain_id="0xa4b1",
                                     is_mainnet=True)
        self.assertEqual(td.hash_typed_data(a), td.hash_typed_data(b))

    def test_validation(self) -> None:
        for kw in ({"nonce_ms": 5}, {"nonce_ms": NONCE, "signature_chain_id": 0},
                   {"nonce_ms": NONCE, "signature_chain_id": "arb"}):
            with self.assertRaises(ValidationFailed):
                td.approve_agent_request(AGENT, **{"signature_chain_id": ARB, **kw})
        with self.assertRaises(ValidationFailed):
            td.approve_builder_fee_request("0x12", nonce_ms=NONCE, signature_chain_id=ARB)
        with self.assertRaises(ValidationFailed):
            td.user_signed_typed_data(td.USD_SEND_PRIMARY, td.USD_SEND_TYPES, {"amount": "1"}, ARB)


class SignAndPost(unittest.TestCase):
    def test_signature_roundtrip_and_exchange_body(self) -> None:
        priv = int.from_bytes(keccak256(b"aijalon test user"), "big")
        user = _address(_mul(priv, _G))
        req = td.approve_builder_fee_request(BUILDER, nonce_ms=NONCE, signature_chain_id=ARB)
        digest = td.hash_typed_data(req.typed_data)
        r, s, v = _sign(digest, priv, k=0x1234567890ABCDEF)
        self.assertEqual(_recover(digest, r, s, v), user)
        body = td.exchange_payload(req.action, {"r": hex(r), "s": hex(s), "v": v})
        self.assertEqual(body["nonce"], NONCE)
        self.assertEqual(body["action"], req.action)
        self.assertEqual(body["signature"]["v"], v)
        # a different nonce in the action changes the digest (no replay of old signatures)
        other = td.approve_builder_fee_request(BUILDER, nonce_ms=NONCE + 1, signature_chain_id=ARB)
        self.assertNotEqual(td.hash_typed_data(other.typed_data), digest)
        send = td.usd_send_request(TREASURY, "10", time_ms=NONCE, signature_chain_id=ARB)
        self.assertEqual(td.exchange_payload(send.action, {"r": "0x1", "s": "0x2", "v": 27})["nonce"], NONCE)

    def test_exchange_payload_rejects(self) -> None:
        req = td.approve_agent_request(AGENT, nonce_ms=NONCE, signature_chain_id=ARB)
        for sig in ({"r": "0x1", "s": "0x2", "v": 29}, {"r": 1, "s": "0x2", "v": 27}, {"r": "0x1", "s": "zz", "v": 27},
                    {"r": "0x1", "s": "0x2", "v": True}):
            with self.assertRaises(ValidationFailed):
                td.exchange_payload(req.action, sig)
        with self.assertRaises(ValidationFailed):
            td.exchange_payload({"type": "order"}, {"r": "0x1", "s": "0x2", "v": 27})


if __name__ == "__main__":
    unittest.main()
