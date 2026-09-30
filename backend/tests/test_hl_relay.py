"""/exchange relay fallback (app/hl/relay.py): only user-signed approveAgent / approveBuilderFee / usdSend (to the
treasury) signed by one of the caller's VERIFIED wallets are relayed, field by field against server expectations;
everything else is refused before any network call. The forwarder sends the body unchanged, once, never follows a
redirect. Signatures are produced here with a tiny secp256k1 signer (stdlib) over app.hl.typed_data's EIP-712 hash.
"""
from __future__ import annotations

import io
import json
import secrets
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import ethsig  # noqa: E402
from app.errors import ExternalServiceError, ValidationFailed  # noqa: E402
from app.hl import typed_data as td  # noqa: E402
from app.hl.relay import RelayPolicy, RelayRefused, fee_rate_tenths_bp, forward_exchange, validate_relay  # noqa: E402
from app.security.keccak import keccak256  # noqa: E402

NOW = 1_790_000_000_000
BUILDER = "0x" + "b1" * 20
TREASURY = "0x" + "7e" * 20
POLICY = RelayPolicy(hyperliquid_chain="Mainnet", builder_address=BUILDER, max_builder_fee_tenths_bp=100,
                     treasury_address=TREASURY)


class Key:
    """secp256k1 test key: address + EIP-712 signing (low-s, v ∈ {27, 28})."""

    def __init__(self) -> None:
        self.d = secrets.randbelow(ethsig._N - 1) + 1
        x, y = ethsig._mul(self.d, (ethsig._GX, ethsig._GY))
        self.address = "0x" + keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:].hex()

    def sign(self, digest: bytes, *, high_s: bool = False) -> dict:
        n = ethsig._N
        e = int.from_bytes(digest, "big") % n
        while True:
            k = secrets.randbelow(n - 1) + 1
            rx, ry = ethsig._mul(k, (ethsig._GX, ethsig._GY))
            r = rx % n
            s = pow(k, -1, n) * (e + r * self.d) % n
            if r and s:
                break
        parity = ry & 1
        if s > n // 2:
            s, parity = n - s, parity ^ 1
        if high_s:
            s, parity = n - s, parity ^ 1
        return {"r": hex(r), "s": hex(s), "v": 27 + parity}


def signed(key: Key, req: td.UserSignedRequest, **kw) -> dict:
    return {"action": dict(req.action), "nonce": req.nonce, "signature": key.sign(td.hash_typed_data(req.typed_data), **kw)}


class RelayValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.master = Key()
        self.agent = "0x" + "a9" * 20
        self.pending = {self.agent: {"master_address": self.master.address, "agent_name": "aijalon"}}
        self.wallets = [self.master.address]

    def check(self, body, **kw):
        return validate_relay(body, policy=kw.pop("policy", POLICY), now_ms=kw.pop("now_ms", NOW),
                              verified_wallets=kw.pop("wallets", self.wallets),
                              pending_agents=kw.pop("pending", self.pending))

    def approve_agent(self, **kw):
        return td.approve_agent_request(kw.pop("agent", self.agent), nonce_ms=kw.pop("nonce", NOW - 1000),
                                        signature_chain_id="0xa4b1", agent_name=kw.pop("name", "aijalon"), **kw)

    # ---------------------------------------------------------------------------------------------- accepted
    def test_three_allowed_kinds_pass_and_body_is_unchanged(self):
        b1 = signed(self.master, self.approve_agent())
        c1 = self.check(b1)
        self.assertEqual((c1.kind, c1.signer), ("approveAgent", self.master.address))
        self.assertEqual(c1.body, b1)
        b2 = signed(self.master, td.approve_builder_fee_request(BUILDER, nonce_ms=NOW, signature_chain_id="0x66eee",
                                                                max_fee_tenths_bp=100))
        c2 = self.check(b2)
        self.assertEqual((c2.kind, c2.detail["max_fee_tenths_bp"]), ("approveBuilderFee", 100))
        self.assertEqual(c2.body, b2)
        b3 = signed(self.master, td.usd_send_request(TREASURY, "25.5", time_ms=NOW + 60_000, signature_chain_id="0xa4b1"))
        c3 = self.check(b3)
        self.assertEqual((c3.kind, c3.detail["amount"]), ("usdSend", "25.5"))
        self.assertEqual(json.dumps(c3.body), json.dumps(b3))                  # key order preserved too

    def test_lower_builder_fee_is_fine(self):
        b = signed(self.master, td.approve_builder_fee_request(BUILDER, nonce_ms=NOW, signature_chain_id="0xa4b1",
                                                               max_fee_tenths_bp=50))
        self.assertEqual(self.check(b).detail["max_fee_tenths_bp"], 50)

    # ---------------------------------------------------------------------------------------------- refused
    def test_never_relays_other_action_types(self):
        sig = {"r": "0x1", "s": "0x1", "v": 27}
        for action in ({"type": "order", "orders": [], "grouping": "na"},
                       {"type": "cancel", "cancels": []},
                       {"type": "withdraw3", "hyperliquidChain": "Mainnet", "signatureChainId": "0xa4b1",
                        "destination": self.master.address, "amount": "1", "time": NOW},
                       {"type": "usdClassTransfer", "hyperliquidChain": "Mainnet", "signatureChainId": "0xa4b1",
                        "amount": "1", "toPerp": True, "nonce": NOW},
                       {"type": "spotSend", "hyperliquidChain": "Mainnet", "signatureChainId": "0xa4b1",
                        "destination": TREASURY, "token": "USDC", "amount": "1", "time": NOW},
                       {"type": "vaultTransfer", "vaultAddress": TREASURY, "isDeposit": True, "usd": 1},
                       {"type": "approveAgent"}):
            with self.subTest(t=action["type"]):
                with self.assertRaises(RelayRefused):
                    self.check({"action": action, "nonce": NOW, "signature": sig})

    def test_shape_chain_nonce_and_signature_checks(self):
        good = signed(self.master, self.approve_agent())
        cases = {
            "vaultAddress": {**good, "vaultAddress": TREASURY},
            "expiresAfter": {**good, "expiresAfter": NOW},
            "extra action field": {**good, "action": {**good["action"], "extra": 1}},
            "missing field": {**good, "action": {k: v for k, v in good["action"].items() if k != "agentName"}},
            "nonce mismatch": {**good, "nonce": good["nonce"] + 1},
            "bad v": {**good, "signature": {**good["signature"], "v": 29}},
            "bool v": {**good, "signature": {**good["signature"], "v": True}},
            "bad chain id": {**good, "action": {**good["action"], "signatureChainId": "0x0"}},
        }
        for name, body in cases.items():
            with self.subTest(name):
                with self.assertRaises(RelayRefused):
                    self.check(body)
        with self.assertRaises(RelayRefused):                                        # testnet action on mainnet
            self.check(signed(self.master, td.approve_agent_request(self.agent, nonce_ms=NOW, signature_chain_id="0xa4b1",
                                                                    is_mainnet=False)))
        with self.assertRaises(RelayRefused):                                        # stale
            self.check(signed(self.master, self.approve_agent(nonce=NOW - 16 * 60_000)))
        with self.assertRaises(RelayRefused):                                        # too far ahead
            self.check(signed(self.master, self.approve_agent(nonce=NOW + 6 * 60_000)))
        with self.assertRaises(RelayRefused):                                        # high-s (malleable) signature
            self.check(signed(self.master, self.approve_agent(), high_s=True))
        tampered = signed(self.master, self.approve_agent())
        tampered["action"]["agentName"] = "aijalon2"
        self.pending[self.agent]["agent_name"] = "aijalon2"
        with self.assertRaises(RelayRefused):                                        # signed fields changed
            self.check(tampered)

    def test_agent_must_be_callers_pending_agent_approved_by_its_master(self):
        with self.assertRaises(RelayRefused):
            self.check(signed(self.master, self.approve_agent(agent="0x" + "a8" * 20)))       # not pending for caller
        with self.assertRaises(RelayRefused):
            self.check(signed(self.master, self.approve_agent(name="other")))                # wrong agent name
        other = Key()                                                                   # caller's other wallet
        with self.assertRaises(RelayRefused):
            self.check(signed(other, self.approve_agent()), wallets=[self.master.address, other.address])
        with self.assertRaises(RelayRefused):                                        # signer not a verified wallet
            self.check(signed(Key(), self.approve_agent()))

    def test_builder_and_fee_cap(self):
        with self.assertRaises(RelayRefused):
            self.check(signed(self.master, td.approve_builder_fee_request("0x" + "b2" * 20, nonce_ms=NOW,
                                                                          signature_chain_id="0xa4b1")))
        tight = RelayPolicy(hyperliquid_chain="Mainnet", builder_address=BUILDER, max_builder_fee_tenths_bp=50,
                            treasury_address=TREASURY)
        with self.assertRaises(RelayRefused):
            self.check(signed(self.master, td.approve_builder_fee_request(BUILDER, nonce_ms=NOW, signature_chain_id="0xa4b1",
                                                                          max_fee_tenths_bp=100)), policy=tight)
        body = signed(self.master, td.approve_builder_fee_request(BUILDER, nonce_ms=NOW, signature_chain_id="0xa4b1"))
        body["action"]["maxFeeRate"] = "0.10001%"                                     # rounds UP above the cap
        with self.assertRaises(RelayRefused):
            self.check(body)

    def test_usd_send_only_to_treasury(self):
        with self.assertRaises(RelayRefused):
            self.check(signed(self.master, td.usd_send_request("0x" + "7f" * 20, "10", time_ms=NOW,
                                                               signature_chain_id="0xa4b1")))
        body = signed(self.master, td.usd_send_request(TREASURY, "10", time_ms=NOW, signature_chain_id="0xa4b1"))
        body["action"]["amount"] = "-1"
        with self.assertRaises(RelayRefused):
            self.check(body)
        no_treasury = RelayPolicy(hyperliquid_chain="Mainnet", builder_address=BUILDER, max_builder_fee_tenths_bp=100,
                                  treasury_address="")
        with self.assertRaises(RelayRefused):
            self.check(signed(self.master, td.usd_send_request(TREASURY, "10", time_ms=NOW, signature_chain_id="0xa4b1")),
                       policy=no_treasury)

    def test_refusal_is_a_validation_error(self):
        self.assertTrue(issubclass(RelayRefused, ValidationFailed))

    def test_fee_rate_parse(self):
        self.assertEqual([fee_rate_tenths_bp(x) for x in ("0.1%", "0.01%", "0.001%", "0.05%", "0.1000001%", "1%")],
                         [100, 10, 1, 50, 101, 1000])
        self.assertIsNone(fee_rate_tenths_bp("0.1"))
        self.assertIsNone(fee_rate_tenths_bp("abc%"))


class Resp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._b = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass


class ForwardTest(unittest.TestCase):
    URL = "https://api.hyperliquid.xyz/exchange"

    def test_posts_body_once_unchanged(self):
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, req.get_method(), req.data, dict(req.header_items()), timeout))
            return Resp(200, b'{"status":"ok","response":{"type":"default"}}')
        body = {"action": {"type": "usdSend", "amount": "1"}, "nonce": 1, "signature": {"r": "0x1", "s": "0x2", "v": 27}}
        status, out = forward_exchange(self.URL, body, opener=opener)
        self.assertEqual((status, out), (200, {"status": "ok", "response": {"type": "default"}}))
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0][0], seen[0][1]), (self.URL, "POST"))
        self.assertEqual(json.loads(seen[0][2]), body)

    def test_upstream_error_status_is_returned(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(self.URL, 422, "bad", {}, io.BytesIO(b"Failed to deserialize"))
        self.assertEqual(forward_exchange(self.URL, {"a": 1}, opener=opener), (422, "Failed to deserialize"))

    def test_network_error_and_bad_target(self):
        def opener(req, timeout):
            raise urllib.error.URLError("down")
        with self.assertRaises(ExternalServiceError):
            forward_exchange(self.URL, {"a": 1}, opener=opener)
        for bad in ("http://api.hyperliquid.xyz/exchange", "https://api.hyperliquid.xyz/info"):
            with self.assertRaises(ValidationFailed):
                forward_exchange(bad, {"a": 1}, opener=opener)

    def test_response_cap(self):
        def opener(req, timeout):
            return Resp(200, b"x" * 100)
        with self.assertRaises(ExternalServiceError):
            forward_exchange(self.URL, {"a": 1}, opener=opener, max_bytes=10)


if __name__ == "__main__":
    unittest.main()
